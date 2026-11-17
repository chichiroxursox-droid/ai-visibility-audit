"""AIO-Citation Kit · Deliverable 1 — the Directory-Gap List.

Turns a business's audit into a ranked "get listed here" plan. The gold input is
`aio.engines[*].cited_domains[]`: the exact third-party sites each AI assistant pulled from when
asked "best <trade> in <city>". A site the AI already trusts for this query, that the business
ISN'T on, is the highest-ROI citation gap there is.

Fully deterministic: it joins the audit's cited domains against a curated directory map
(`assets/directories.json`) — no LLM, no fabricated data. Output is split into compliance-aware
sections (claim-free-first, trade-specific, paid, participation, not-claimable) so the operator
never, e.g., tries to "claim" a read-only aggregator or spams social platforms.

CLI:  python -m tools.directory_gap --from .tmp/<slug>/audit.json
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any
from urllib.parse import urlparse

from tools import common

MAP_PATH = common.ROOT / "assets" / "directories.json"

ENGINE_LABELS = {"chatgpt": "ChatGPT", "perplexity": "Perplexity", "google_ai_overview": "Google AI Overview"}


def _load_map() -> list[dict[str, Any]]:
    return json.loads(MAP_PATH.read_text(encoding="utf-8")).get("directories", [])


def _norm_domain(value: str | None) -> str | None:
    """Lowercase registrable-ish domain: strip scheme + leading www."""
    if not value:
        return None
    v = value.strip().lower()
    if "://" not in v:
        v = "https://" + v
    net = urlparse(v).netloc or urlparse(v).path
    net = net.split("/")[0]
    return net[4:] if net.startswith("www.") else net or None


def _match_entry(domain: str, entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the map entry for a cited domain (exact or subdomain, e.g. biz.yelp.com -> yelp.com)."""
    for e in entries:
        d = e["domain"]
        if domain == d or domain.endswith("." + d):
            return e
    return None


def _derive_trade(audit: dict[str, Any]) -> str:
    """The consumer-search trade. Prefer the category the AIO check actually asked about
    ('best <trade> in <city>') over Google's clunky primary category."""
    q = ((audit.get("aio", {}) or {}).get("queries") or [None])[0] or ""
    m = re.match(r"\s*best\s+(.+?)\s+in\s+", q, re.I)
    if m:
        return m.group(1).strip().lower()
    b = audit.get("business", {}) or {}
    return (b.get("search_category") or b.get("primary_category") or "").strip().lower()


def _trade_matches(trade: str, trade_list: list[str]) -> bool:
    """Tolerant trade match so 'brake repair' hits a list entry 'brake repair' and 'auto repair'
    loosely matches 'auto repair shop' (substring either direction)."""
    if not trade or not trade_list:
        return False
    return any(t.lower() == trade or t.lower() in trade or trade in t.lower() for t in trade_list)


def _free_bonus(free: bool, effort: int) -> int:
    if not free:
        return 0
    return 3 if effort < 15 else 2


def _pipeline_bonus(entry: dict[str, Any]) -> int:
    """AI-pipeline weight: engines that cite this source by default (e.g. ChatGPT's local answers
    run through Bing Places + Facebook/Yelp reviews) outrank filler directories even when the
    sampled answers didn't happen to cite them."""
    return 2 * len(entry.get("cited_by_default") or [])


def _blockers(entry: dict[str, Any], business: dict[str, Any]) -> list[str]:
    reqs = entry.get("requirements") or []
    out: list[str] = []
    if "ein_or_tax_id" in reqs:
        out.append("Needs an EIN or Federal Taxpayer ID to verify.")
    if "license" in reqs:
        out.append("Needs a trade license on file.")
    if "background_check" in reqs:
        out.append("Requires a background check (paid platform).")
    if "business_docs_for_verified" in reqs:
        out.append("A Verified badge may require business documents (license/EIN).")
    if "phone" in reqs and not business.get("phone"):
        out.append("Needs a business phone number (none found on Google).")
    return out


def _action(entry: dict[str, Any], status: str) -> str:
    cat = entry.get("category")
    if cat == "aggregator_readonly":
        if entry["domain"] == "carfax.com":
            return "Read-only: make sure your shop software reports service records to Carfax (no profile to claim)."
        return "Read-only source — you can't claim it; AIs surface it editorially. Informational only."
    if cat == "social":
        return "Participate authentically (answer real local/trade questions, link your service pages). Not a profile to 'claim'; never spam or use fake accounts."
    verify = {"video": "video verification", "phone_pin": "phone PIN", "phone_or_email": "phone/email",
              "phone_email_or_postcard": "phone/email/postcard", "tax_id": "EIN/Tax ID + Apple ID",
              "account": "account login"}.get(entry.get("verification"), entry.get("verification") or "")
    paid = "" if entry.get("free") else " (paid platform)"
    if status == "cited-but-unclaimed":
        return f"AI already cites this for your query — claim/optimize your profile ({verify}, ~{entry.get('effort_minutes')} min){paid}."
    return f"Create + claim your profile ({verify}, ~{entry.get('effort_minutes')} min){paid}."


def build_gap_list(audit: dict[str, Any]) -> dict[str, Any]:
    """Build the ranked directory-gap plan. Returns the structure the renderer/JSON consumer use."""
    entries = _load_map()
    business = audit.get("business", {}) or {}
    trade = _derive_trade(audit)
    own_domain = _norm_domain(business.get("website"))

    aio = audit.get("aio", {}) or {}
    engines = aio.get("engines", {}) or {}

    # 1. cited domain -> set of engines that cited it (deterministic, per-engine kept separate)
    cited: dict[str, set[str]] = {}
    for ekey, ev in engines.items():
        for d in (ev.get("cited_domains") or []):
            nd = _norm_domain(d)
            if nd:
                cited.setdefault(nd, set()).add(ekey)

    rows: list[dict[str, Any]] = []
    own_site_cited_by: list[str] = []
    seen_entry_domains: set[str] = set()

    # 2. classify every domain the AIs actually cited
    for domain, eng_set in cited.items():
        if own_domain and (domain == own_domain or domain.endswith("." + own_domain) or own_domain.endswith("." + domain)):
            own_site_cited_by = sorted(ENGINE_LABELS.get(e, e) for e in eng_set)
            continue
        entry = _match_entry(domain, entries)
        freq = len(eng_set)
        if entry is None:
            # Unknown cited source (a competitor site, blog, or niche listicle). Informational only.
            rows.append({
                "platform": domain, "domain": domain, "category": "unknown_cited",
                "cited_by_engines": sorted(ENGINE_LABELS.get(e, e) for e in eng_set),
                "claim_url": None, "verification": None, "effort_minutes": None,
                "free": None, "claimable": False, "status": "cited-not-in-map",
                "trade_specific": False, "score": freq * 3,
                "action": "An AI pulled from this site for your query. If it's a directory you can join, add it to the map; if it's a competitor/blog, it's a content/PR target.",
                "blockers": [],
            })
            continue
        seen_entry_domains.add(entry["domain"])
        claimable = bool(entry.get("claimable"))
        status = "cited-but-unclaimed" if claimable else "cited-not-claimable"
        trade_match = _trade_matches(trade, entry.get("trade_specific_for") or [])
        score = freq * 3 + (5 if status == "cited-but-unclaimed" else 0) + (4 if trade_match else 0) \
            + _pipeline_bonus(entry) \
            + _free_bonus(bool(entry.get("free")), entry.get("effort_minutes") or 0) - (999 if not claimable else 0)
        rows.append(_row(entry, eng_set, status, trade_match, score, business))

    # 3. add map entries the AIs DIDN'T cite but the business should still be on
    #    (universal Tier-1 claimables + trade-specific claimables for this trade)
    for entry in entries:
        if entry["domain"] in seen_entry_domains or not entry.get("claimable"):
            continue
        universal = entry.get("category") in {"directory", "review"} and entry.get("free") \
            and (entry.get("cited_by_default") or entry["domain"] in {"google.com", "yelp.com", "bingplaces.com", "apple.com", "bbb.org"})
        trade_match = _trade_matches(trade, entry.get("trade_specific_for") or [])
        if not (universal or trade_match):
            continue
        score = (4 if trade_match else 0) + _pipeline_bonus(entry) \
            + _free_bonus(bool(entry.get("free")), entry.get("effort_minutes") or 0)
        rows.append(_row(entry, set(), "unclaimed", trade_match, score, business))

    # 4. bucket into compliance-aware sections
    sections = {"claim_free_first": [], "trade_specific": [], "consider_paid": [],
                "participation": [], "not_claimable": [], "other_cited_sources": []}
    for r in rows:
        cat = r["category"]
        if cat == "unknown_cited":
            sections["other_cited_sources"].append(r)   # competitor sites / blogs the AI pulled from
        elif cat == "social":
            sections["participation"].append(r)
        elif cat == "aggregator_readonly" or not r["claimable"]:
            sections["not_claimable"].append(r)
        elif r.get("free") is False:
            sections["consider_paid"].append(r)
        elif r["trade_specific"]:
            sections["trade_specific"].append(r)
        else:
            sections["claim_free_first"].append(r)
    for k in sections:
        sections[k].sort(key=lambda x: (-(x.get("score") or 0), x.get("effort_minutes") or 999))

    n_cited = len(cited)
    n_claimable_gaps = sum(len(sections[k]) for k in ("claim_free_first", "trade_specific", "consider_paid"))
    return {
        "trade": trade or None,
        "own_site_cited_by": own_site_cited_by,
        "summary": {
            "sources_ai_cited": n_cited,
            "claimable_gaps": n_claimable_gaps,
            "cited_but_unclaimed": sum(1 for r in rows if r["status"] == "cited-but-unclaimed"),
        },
        "sections": sections,
    }


def _row(entry: dict[str, Any], eng_set: set[str], status: str, trade_match: bool,
         score: int, business: dict[str, Any]) -> dict[str, Any]:
    return {
        "platform": entry.get("display_name"), "domain": entry["domain"], "category": entry.get("category"),
        "cited_by_engines": sorted(ENGINE_LABELS.get(e, e) for e in eng_set),
        "claim_url": entry.get("claim_url"), "verification": entry.get("verification"),
        "effort_minutes": entry.get("effort_minutes"), "free": entry.get("free"),
        "claimable": bool(entry.get("claimable")), "status": status, "trade_specific": trade_match,
        "score": score, "action": _action(entry, status), "blockers": _blockers(entry, business),
        "notes": entry.get("notes"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Directory-Gap List from an audit.json.")
    ap.add_argument("--from", dest="src", required=True)
    args = ap.parse_args()
    audit = common.load_json(args.src)
    gap = build_gap_list(audit)
    print(json.dumps(gap, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
