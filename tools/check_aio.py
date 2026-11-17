"""Phase 3 — the AIO money-shot. Fills the `aio` section of the audit.

We ask three AI surfaces the question a real customer would ask — "best <category> in <city>" —
and record, for each: was the business mentioned? which competitors were mentioned instead? and
which sources did the AI cite? (The cited domains are gold: they're the exact directories/sites to
get the client onto.)

Engines:
  - ChatGPT  -> OpenAI Responses API + the `web_search` tool (live web), parse url_citation annotations.
  - Perplexity -> sonar models (citations returned by default).
  - Google AI Overview -> SerpAPI (`ai_overview` block + `references`), with the page_token follow-up.

All three are independent and individually optional: a missing key or failure records an error and
leaves that engine out — the rest still run. AI answers are non-deterministic and location-dependent,
so each result is stamped as a dated snapshot.

CLI:  python -m tools.check_aio --name "Joe's Plumbing" --city "Austin, TX" --category "Plumber"
"""

from __future__ import annotations

import argparse
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse

import requests

from tools import common

COST_OPENAI = 0.05
COST_PERPLEXITY = 0.01
COST_SERPAPI = 0.01

# Multi-sample config. One run of one prompt is a coin flip (AI answers vary 40-60% run-to-run); we
# sample a small PORTFOLIO of phrasings, each REPEATED, and report an appearance RATE + stability
# instead of a yes/no. Total calls per engine = len(portfolio) * repeats. Keep it cost-capped.
DEFAULT_REPEATS = int(os.environ.get("AIO_REPEATS", "2"))   # times each prompt is re-asked
AIO_MAX_WORKERS = int(os.environ.get("AIO_MAX_WORKERS", "6"))
TREND_ONLY_BELOW = 30   # per-engine samples below this = "trend only, not a hard metric" (spec §5)

ENGINE_KEYS = ["chatgpt", "perplexity", "google_ai_overview"]
ENGINE_KEY_ENV = {"chatgpt": "OPENAI_API_KEY", "perplexity": "PERPLEXITY_API_KEY",
                  "google_ai_overview": "SERPAPI_KEY"}
ENGINE_KEY_NAME = {"chatgpt": "openai", "perplexity": "perplexity", "google_ai_overview": "serpapi"}
ENGINE_COST = {"chatgpt": COST_OPENAI, "perplexity": COST_PERPLEXITY, "google_ai_overview": COST_SERPAPI}
ENGINE_COST_LABEL = {"chatgpt": "openai_web_search", "perplexity": "perplexity_sonar",
                     "google_ai_overview": "serpapi"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _domain(url: str) -> str | None:
    try:
        net = urlparse(url).netloc.lower()
        return net[4:] if net.startswith("www.") else net or None
    except Exception:
        return None


def _domains_from(urls: list[str]) -> list[str]:
    seen, out = set(), []
    for u in urls:
        d = _domain(u)
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


# Words too generic to identify a specific business on their own — never match on these alone.
_GENERIC = {"mobile", "brake", "brakes", "repair", "auto", "automotive", "car", "cars",
            "tire", "tires", "service", "services", "shop", "center", "centre", "co", "inc",
            "llc", "company", "plumbing", "plumber", "hvac", "electric", "electrical",
            "roofing", "the", "and", "of", "for", "your"}


def _recommended(name: str, text: str) -> bool:
    """True only if the business is NAMED in the answer text (an actual recommendation).

    Deliberately does NOT count a domain appearing among the AI's cited sources — that is a
    weaker, different signal (the AI read their page but may recommend competitors instead),
    tracked separately by `_cited_as_source`. Conflating the two produces false positives that
    would tell an invisible business it's "recommended" when it isn't.
    """
    t = _norm(text)
    n = _norm(name)
    if not n or not t:
        return False
    if n in t:                                   # the full business name appears verbatim
        return True
    toks = [w for w in n.split() if w]
    distinctive = [w for w in toks if len(w) >= 4 and w not in _GENERIC]
    if not distinctive:
        return False                              # nothing specific enough to match safely
    first = toks[0]
    # The proper-noun head (e.g. "joes") must appear as a whole word when it's distinctive.
    if first in distinctive and not re.search(rf"\b{re.escape(first)}\b", t):
        return False
    # A single distinctive token (e.g. "Joe's <generic words>") is enough on its own;
    # otherwise require at least two distinctive tokens to appear, to avoid false matches.
    if first in distinctive and len(distinctive) == 1:
        return True
    hits = sum(1 for w in distinctive if re.search(rf"\b{re.escape(w)}\b", t))
    return hits >= 2 if len(distinctive) >= 2 else hits >= 1


def _cited_as_source(website: str | None, cited_domains: list[str]) -> bool:
    """True if the business's own domain is among the AI's cited sources — a weaker signal than
    being recommended by name (the AI pulled their page but may still endorse competitors)."""
    if not website:
        return False
    d = _domain(website if website.startswith("http") else "https://" + website)
    return bool(d and d in cited_domains)


def _mention_snippet(name: str, text: str, width: int = 260) -> str | None:
    """A short quote around the first place the business is named — the report's honest 'money shot'
    (so we never display an answer excerpt that doesn't actually contain the business)."""
    if not text:
        return None
    # Anchor on the raw name's alpha-runs (e.g. "Joe's Mobile" -> "Joe","Mobile") so possessives
    # match: searching normalized "joes" against raw "Joe's" would miss because of the apostrophe.
    words = re.findall(r"[A-Za-z]{4,}", name)
    anchors = [w.lower() for w in words if w.lower() not in _GENERIC] or [w.lower() for w in words]
    low = text.lower()
    pos = -1
    for a in anchors:
        m = re.search(rf"\b{re.escape(a)}", low)
        if m:
            pos = m.start()
            break
    if pos < 0:
        return None
    s = max(0, pos - width // 2)
    e = min(len(text), pos + width // 2)
    snip = " ".join(text[s:e].split())
    return ("…" if s > 0 else "") + snip + ("…" if e < len(text) else "")


def _competitors_in(text: str, competitors: list[dict[str, Any]]) -> list[str]:
    t = _norm(text)
    found = []
    for c in competitors:
        cn = _norm(c.get("name") or "")
        if cn and len(cn) >= 4 and cn in t:
            found.append(c["name"])
    return found


def _blank(asked: bool = True) -> dict[str, Any]:
    # 'present' defaults True (chat engines always return an answer); the Google AI Overview
    # path overrides it to False when no overview is shown, so all engines share one contract.
    return {"asked": asked, "mentioned": False, "recommended": False, "cited_as_source": False,
            "mention_snippet": None, "raw_answer": "", "cited_domains": [],
            "competitors_mentioned": [], "present": True, "error": None}


# ---------------------------------------------------------------------------
# engines
# ---------------------------------------------------------------------------

def query_openai(prompt: str) -> dict[str, Any]:
    res = _blank()
    key = common.get_key("openai")
    if not key:
        res.update(asked=False, error="OPENAI_API_KEY not set")
        return res
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        last_err: Exception | None = None
        resp = None
        for tool_type in ("web_search", "web_search_preview"):
            try:
                resp = client.responses.create(model="gpt-4o", tools=[{"type": tool_type}], input=prompt)
                break
            except Exception as e:  # noqa: BLE001 - SDK tool-name drift; try the alias
                last_err = e
        if resp is None:
            raise last_err or RuntimeError("web_search tool unavailable")

        res["raw_answer"] = getattr(resp, "output_text", "") or ""
        urls: list[str] = []
        dump = resp.model_dump()
        for item in dump.get("output", []) or []:
            for c in item.get("content", []) or []:
                for ann in c.get("annotations", []) or []:
                    if ann.get("type") == "url_citation" and ann.get("url"):
                        urls.append(ann["url"])
        res["cited_domains"] = _domains_from(urls)
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
    return res


def query_perplexity(prompt: str) -> dict[str, Any]:
    res = _blank()
    key = common.get_key("perplexity")
    if not key:
        res.update(asked=False, error="PERPLEXITY_API_KEY not set")
        return res
    try:
        resp = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "sonar", "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        msg = ((data.get("choices") or [{}])[0] or {}).get("message", {}) or {}
        res["raw_answer"] = msg.get("content", "")
        # Perplexity has returned citations/search_results at the top level historically and under
        # message in some responses — read both so we never silently drop the cited sources.
        urls = list(data.get("citations") or msg.get("citations") or [])
        for sr in (data.get("search_results") or msg.get("search_results") or []):
            if isinstance(sr, dict) and sr.get("url"):
                urls.append(sr["url"])
            elif isinstance(sr, str):
                urls.append(sr)
        res["cited_domains"] = _domains_from(urls)
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {getattr(e, 'response', None) and e.response.text[:200] or e}"
    return res


def _local_pack_rank(data: dict[str, Any], name: str | None) -> tuple[int | None, list[str]]:
    """Where Google's local map pack ranks the business + the top names. For local 'best X' queries
    Google usually answers with the pack (not an AI Overview), so this keeps 'no overview' from being
    misreported as 'invisible on Google.'"""
    lr = data.get("local_results")
    places = (lr.get("places") if isinstance(lr, dict) else lr) or []
    top = [p.get("title") for p in places if isinstance(p, dict) and p.get("title")]
    rank = None
    if name:
        n = _norm(name)
        for i, t in enumerate(top, 1):
            nt = _norm(t)
            if nt and (n in nt or nt in n):
                rank = i
                break
    return rank, top[:5]


def _organic_rank(data: dict[str, Any], name: str | None,
                  website: str | None) -> tuple[int | None, str | None, list[str]]:
    """Where the business ranks in classic organic (blue-link) results, and who's winning them.
    The upstream lever: AI Overviews almost never cite a site outside the top ~10-20 organic, so
    'not cited' usually traces back to 'doesn't rank.' Match the business's OWN domain first (that's
    real ranking); fall back to its name in a result title (often a directory that lists them)."""
    results = data.get("organic_results") or []
    site_dom = _domain(website if website.startswith("http") else "https://" + website) if website else None
    n = _norm(name) if name else None
    top: list[str] = []
    rank: int | None = None
    match: str | None = None
    for r in results:
        if not isinstance(r, dict):
            continue
        link_dom = _domain(r.get("link", "") or "")
        if link_dom and link_dom not in top:
            top.append(link_dom)
        if rank is not None:
            continue                              # keep filling `top`, but the rank is locked in
        pos = r.get("position")
        title = _norm(r.get("title", "") or "")
        if site_dom and link_dom == site_dom:
            rank, match = pos, "domain"
        elif n and title and (n in title or title in n):
            rank, match = pos, "name"
    return rank, match, top[:5]


def query_google_ai_overview(query: str, location: str, name: str | None = None,
                             website: str | None = None) -> dict[str, Any]:
    res = _blank()
    res["present"] = False
    res["served"] = False                  # did Google actually return an AI Overview for this query?
    key = common.get_key("serpapi")
    if not key:
        res.update(asked=False, error="SERPAPI_KEY not set")
        return res
    try:
        from serpapi import GoogleSearch
        data = GoogleSearch({"api_key": key, "engine": "google", "q": query,
                             "location": location, "gl": "us", "hl": "en"}).get_dict()
        # Capture the local-pack rank regardless — it's Google's real answer when no overview shows.
        res["local_pack_rank"], res["local_pack_top"] = _local_pack_rank(data, name)
        # And the classic organic (blue-link) rank — the upstream lever that drives AI-Overview citation.
        res["organic_rank"], res["organic_match"], res["organic_top"] = _organic_rank(data, name, website)

        ai = data.get("ai_overview")
        # Always follow "Show more": a collapsed overview hides the rest behind a page_token (the inline
        # SERP shows only a preview). Fetch the full version whenever a token is present.
        if ai and ai.get("page_token"):
            try:
                d2 = GoogleSearch({"api_key": key, "engine": "google_ai_overview",
                                   "page_token": ai["page_token"]}).get_dict()
                if d2.get("ai_overview"):
                    ai = d2["ai_overview"]      # the fully expanded overview
            except Exception:  # noqa: BLE001 - keep the collapsed preview if expansion fails
                pass

        if not ai:
            return res                          # served stays False — Google answered without an overview
        res["present"] = True
        res["served"] = True

        parts: list[str] = []

        def _walk(blocks: Any) -> None:
            for b in blocks or []:
                if isinstance(b, dict):
                    for fld in ("title", "snippet", "text"):
                        v = b.get(fld)
                        if isinstance(v, str) and v.strip():
                            parts.append(v)
                    for k in ("list", "text_blocks", "items"):
                        if b.get(k):
                            _walk(b[k])
        _walk(ai.get("text_blocks"))
        res["raw_answer"] = "\n".join(parts)
        res["cited_domains"] = _domains_from([r.get("link", "") for r in (ai.get("references") or [])])
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
    return res


# ---------------------------------------------------------------------------
# multi-sample orchestration
# ---------------------------------------------------------------------------

def _portfolio(category: str, city: str) -> list[dict[str, str]]:
    """A small portfolio of how a real customer phrases the same need. queries[0] MUST stay
    'best <cat> in <city>' — downstream tools (directory_gap/schema/faq) parse the trade from it."""
    cat = category.lower()
    return [
        {"id": "best_in_city",
         "search_query": f"best {cat} in {city}",
         "chat_prompt": f"Who is the best {cat} in {city}? Recommend specific local businesses by name."},
        {"id": "near_me",
         "search_query": f"{cat} near me {city}",
         "chat_prompt": (f"I'm in {city} and need a {cat}. Which specific local businesses would you "
                         "recommend? Name them.")},
        {"id": "top_rated",
         "search_query": f"top rated {cat} {city}",
         "chat_prompt": f"What are the top-rated {cat} businesses in {city}? List specific names."},
    ]


def _run_sample(engine: str, item: dict[str, str], city: str, name: str | None = None,
                website: str | None = None) -> dict[str, Any]:
    if engine == "chatgpt":
        return query_openai(item["chat_prompt"])
    if engine == "perplexity":
        return query_perplexity(item["chat_prompt"])
    return query_google_ai_overview(item["search_query"], city, name, website)


def _aggregate_engine(name: str, website: str | None, competitors: list[dict[str, Any]],
                      samples: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Collapse N raw samples for one engine into an appearance RATE + stability, while keeping the
    same per-engine keys the renderer/score/directory_gap already read."""
    res = _blank()
    ok = [(pid, r) for pid, r in samples if not r.get("error")]
    errored = [(pid, r) for pid, r in samples if r.get("error")]
    # "served" = the engine actually produced an AI answer to appear in. Chat engines always do;
    # Google's AI Overview often doesn't for local 'best X' queries (it shows the map pack instead).
    served_ok = [(pid, r) for pid, r in ok if r.get("served", True)]
    not_served = bool(ok) and not served_ok
    local_rank = next((r.get("local_pack_rank") for _, r in ok if r.get("local_pack_rank")), None)
    local_top = next((r.get("local_pack_top") for _, r in ok if r.get("local_pack_top")), None)
    organic_rank = next((r.get("organic_rank") for _, r in ok if r.get("organic_rank")), None)
    organic_match = next((r.get("organic_match") for _, r in ok if r.get("organic_rank")), None)
    organic_top = next((r.get("organic_top") for _, r in ok if r.get("organic_top")), None)

    dom_count: dict[str, int] = {}
    comp_count: dict[str, int] = {}
    by_prompt: dict[str, list[bool]] = {}
    snippet = best_raw = None
    present_any = False
    rec_count = cited_count = 0

    for pid, r in served_ok:
        text = r.get("raw_answer", "") or ""
        rec = _recommended(name, text)
        present_any = present_any or r.get("present", True)
        if rec:
            rec_count += 1
            if snippet is None:
                snippet = _mention_snippet(name, text)
            if best_raw is None:
                best_raw = text
        if _cited_as_source(website, r.get("cited_domains", []) or []):
            cited_count += 1
        for d in (r.get("cited_domains") or []):
            dom_count[d] = dom_count.get(d, 0) + 1
        for c in _competitors_in(text, competitors):
            comp_count[c] = comp_count.get(c, 0) + 1
        by_prompt.setdefault(pid, []).append(rec)

    n = len(served_ok)
    if not_served:
        rec_rate = cited_rate = None         # NA — Google offered no AI answer to appear in
        stability = "not_served"
    elif n:
        rec_rate = round(rec_count / n, 2)
        cited_rate = round(cited_count / n, 2)
        stability = "consistent" if rec_rate == 1 else "absent" if rec_rate == 0 else "intermittent"
    else:
        rec_rate = cited_rate = 0.0
        stability = "no_data"
    if best_raw is None and served_ok:
        best_raw = served_ok[0][1].get("raw_answer", "") or ""
    best_raw = best_raw or ""
    if len(best_raw) > 1500:
        best_raw = best_raw[:1500] + "…"

    res.update(
        asked=True,
        present=present_any,
        recommended=rec_count > 0,
        mentioned=rec_count > 0,
        cited_as_source=cited_count > 0,
        mention_snippet=snippet,
        competitors_mentioned=[c for c, _ in sorted(comp_count.items(), key=lambda kv: (-kv[1], kv[0]))],
        cited_domains=[d for d, _ in sorted(dom_count.items(), key=lambda kv: (-kv[1], kv[0]))],
        raw_answer=best_raw,
        error=None if ok else (errored[0][1].get("error") if errored else "no samples returned"),
        # --- multi-sample fields (the honest part) ---
        samples=n,
        attempts=len(samples),
        sample_errors=len(errored),
        recommended_count=rec_count,
        recommended_rate=rec_rate,
        cited_count=cited_count,
        cited_rate=cited_rate,
        stability=stability,
        served=not not_served,
        local_pack_rank=local_rank,
        local_pack_top=local_top,
        organic_rank=organic_rank,
        organic_match=organic_match,
        organic_top=organic_top,
        per_prompt={pid: {"recommended": sum(1 for b in v if b), "samples": len(v),
                          "rate": round(sum(1 for b in v if b) / len(v), 2)}
                    for pid, v in by_prompt.items()},
    )
    return res


def enrich(audit: dict[str, Any], category: str | None = None,
           repeats: int | None = None) -> dict[str, Any]:
    name = audit["business"].get("resolved_name") or audit["business"]["query_name"]
    website = audit["business"].get("website")
    city = audit["meta"]["query"].get("city") or ""
    competitors = audit.get("competitors", [])
    # Prefer an explicit override, then the trade we resolved in fetch_gbp, then Google's raw category,
    # and finally infer the trade from the business name (covers businesses with no/weak Google profile).
    category = category or audit["business"].get("search_category") or audit["business"].get("primary_category")
    category, _inferred = common.best_category(category, name)

    if common.is_generic_category(category):
        common.record_error(audit, "check_aio",
                            "No specific business category available (Google's is missing or generic, "
                            "and none could be inferred from the name) — skipped the AI-visibility check, "
                            "since 'best services in <city>' is meaningless. Pass --category to run it.")
        return audit

    repeats = max(1, repeats if repeats is not None else DEFAULT_REPEATS)
    portfolio = _portfolio(category, city)
    audit["aio"]["queries"] = [p["search_query"] for p in portfolio]
    samples_per_engine = len(portfolio) * repeats

    have = {ek: bool(common.get_key(ENGINE_KEY_NAME[ek])) for ek in ENGINE_KEYS}

    # Build the flat task list (only for engines we have a key for) and run them concurrently.
    tasks: list[tuple[str, dict[str, str]]] = []
    for item in portfolio:
        for _ in range(repeats):
            for ek in ENGINE_KEYS:
                if have[ek]:
                    tasks.append((ek, item))

    collected: dict[str, list[tuple[str, dict[str, Any]]]] = {ek: [] for ek in ENGINE_KEYS}
    if tasks:
        with ThreadPoolExecutor(max_workers=AIO_MAX_WORKERS) as ex:
            futs = {ex.submit(_run_sample, ek, item, city, name, website): (ek, item["id"]) for ek, item in tasks}
            for fut in as_completed(futs):
                ek, pid = futs[fut]
                try:
                    r = fut.result()
                except Exception as e:  # noqa: BLE001 - one bad sample never sinks the engine
                    r = _blank()
                    r["error"] = f"{type(e).__name__}: {e}"
                collected[ek].append((pid, r))

    out_engines: dict[str, Any] = {}
    for ek in ENGINE_KEYS:
        if not have[ek]:
            blank = _blank(asked=False)
            blank["error"] = f"{ENGINE_KEY_ENV[ek]} not set"
            blank.update(samples=0, recommended_rate=0.0, recommended_count=0, stability="no_key")
            out_engines[ek] = blank
            continue
        # charge per successful sample (errored samples cost nothing)
        for _pid, r in collected[ek]:
            if not r.get("error"):
                common.add_cost(audit, ENGINE_COST_LABEL[ek], ENGINE_COST[ek], "aio multi-sample")
        out_engines[ek] = _aggregate_engine(name, website, competitors, collected[ek])

    checked = sum(1 for ek in ENGINE_KEYS if out_engines[ek].get("asked"))
    mentioned_in = sum(1 for ek in ENGINE_KEYS if out_engines[ek].get("recommended"))
    cited_in = sum(1 for ek in ENGINE_KEYS if out_engines[ek].get("cited_as_source"))
    rated = [out_engines[ek]["recommended_rate"] for ek in ENGINE_KEYS
             if out_engines[ek].get("asked") and out_engines[ek].get("samples")]
    avg_rate = round(sum(rated) / len(rated), 2) if rated else 0.0
    all_domains = [d for ek in ENGINE_KEYS for d in (out_engines[ek].get("cited_domains") or [])]

    audit["aio"]["engines"] = out_engines
    audit["aio"]["summary"] = {
        "mentioned_in": mentioned_in,             # # engines that named the business in >=1 sample
        "recommended_in": mentioned_in,
        "cited_as_source_in": cited_in,
        "engines_checked": checked,
        "all_cited_domains": sorted(set(all_domains)),
        # --- multi-sample summary ---
        "recommended_rate_avg": avg_rate,         # mean appearance rate across engines that gave AI answers
        "samples_per_engine": samples_per_engine,
        "repeats": repeats,
        "prompt_portfolio": audit["aio"]["queries"],
        "trend_only": samples_per_engine < TREND_ONLY_BELOW,
        # Google often serves the local map pack instead of an AI Overview — record where the business
        # ranks there so "no overview" is never misread as "invisible on Google."
        "google_ai_served": out_engines.get("google_ai_overview", {}).get("served"),
        "local_pack_rank": out_engines.get("google_ai_overview", {}).get("local_pack_rank"),
        "local_pack_top": out_engines.get("google_ai_overview", {}).get("local_pack_top"),
        # Classic organic rank for the primary query — AI Overviews rarely cite past the top results,
        # so this is the upstream cause of (non-)citation, not just another metric.
        "organic_rank": out_engines.get("google_ai_overview", {}).get("organic_rank"),
        "organic_match": out_engines.get("google_ai_overview", {}).get("organic_match"),
        "organic_top": out_engines.get("google_ai_overview", {}).get("organic_top"),
    }
    return audit


def main() -> None:
    ap = argparse.ArgumentParser(description="Check AI-assistant visibility for a business (multi-sample).")
    ap.add_argument("--name", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--category", required=True, help="e.g. 'Plumber' (normally comes from fetch_gbp)")
    ap.add_argument("--website", default=None)
    ap.add_argument("--repeats", type=int, default=None,
                    help=f"times each portfolio prompt is re-asked (default {DEFAULT_REPEATS})")
    args = ap.parse_args()

    audit = common.new_audit(args.name, args.city, args.website)
    audit["business"]["primary_category"] = args.category
    audit["business"]["website"] = args.website
    enrich(audit, category=args.category, repeats=args.repeats)

    s = audit["aio"]["summary"]
    print(f"Portfolio: {audit['aio']['queries']}")
    print(f"Samples/engine: {s['samples_per_engine']}  |  avg appearance rate: {s['recommended_rate_avg']:.0%}"
          f"{'  (trend only — small sample)' if s.get('trend_only') else ''}")
    for ek, e in audit["aio"]["engines"].items():
        if not e.get("asked"):
            print(f"  ⊘ {ek}: {e.get('error')}")
            continue
        if e.get("stability") == "not_served":
            lp = f", local-pack #{e.get('local_pack_rank')}" if e.get("local_pack_rank") else ""
            print(f"  ⊘ {ek}: no AI Overview served (Google showed the local pack{lp})")
            continue
        flag = {"consistent": "✅", "intermittent": "🟡", "absent": "❌"}.get(e.get("stability"), "❔")
        pp = " ".join(f"{pid}={d['recommended']}/{d['samples']}" for pid, d in (e.get("per_prompt") or {}).items())
        print(f"  {flag} {ek}: named in {e['recommended_count']}/{e['samples']} ({e['recommended_rate']:.0%}, "
              f"{e['stability']})  [{pp}]  cited={e.get('cited_domains')[:4]} err={e.get('error')}")
    if s.get("organic_rank"):
        print(f"Organic (blue-link) rank for '{audit['aio']['queries'][0]}': #{s['organic_rank']} "
              f"(matched by {s.get('organic_match')})")
    elif s.get("organic_top"):
        print(f"Not in organic top results — blue-link winners: {s.get('organic_top')}")
    print(f"All cited domains: {s['all_cited_domains']}")
    print(f"Est. cost: ${audit['meta']['cost']['total_usd']}")


if __name__ == "__main__":
    main()
