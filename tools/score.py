"""Phase 4 — Claude synthesis. Fills `score`, `narrative`, and `fixes`.

This is the reasoning layer: Claude consumes the assembled audit (Google presence, reviews vs.
competitors, website gaps, AI-assistant visibility) and produces an honest 0-100 visibility score,
a punchy owner-facing narrative, and a prioritized, plain-English fix list mapped to the
done-for-you service.

Model: defaults to claude-opus-4-8 (override with AUDIT_SYNTH_MODEL, e.g. claude-sonnet-4-6 to cut
cost). Uses adaptive thinking + structured output. Falls back to a transparent heuristic score if
the Anthropic key is missing or the call fails, so the engine always produces a renderable report.

CLI:  python -m tools.score --from .tmp/<slug>/audit.json   (or runs heuristic on an empty audit)
"""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any, Literal

from pydantic import BaseModel

from tools import common

MODEL = os.environ.get("AUDIT_SYNTH_MODEL", "claude-opus-4-8")

# Synthesis backend: anthropic (default, paid API) | claude_cli (your Claude Code subscription) |
# groq (free tier) | heuristic (no LLM). Choose with AUDIT_SYNTH_BACKEND.
BACKEND = os.environ.get("AUDIT_SYNTH_BACKEND", "anthropic")
GROQ_MODEL = os.environ.get("AUDIT_GROQ_MODEL", "llama-3.3-70b-versatile")

# Opus 4.8 pricing ($/1M tokens) for accurate cost logging; override if MODEL changes.
PRICE_IN = {"claude-opus-4-8": 5.0, "claude-sonnet-4-6": 3.0, "claude-haiku-4-5": 1.0}
PRICE_OUT = {"claude-opus-4-8": 25.0, "claude-sonnet-4-6": 15.0, "claude-haiku-4-5": 5.0}

JSON_HINT = (
    "Return your answer as a single, strictly-valid JSON object and NOTHING else — no commentary "
    "before or after it. Put it inside ONE ```json code fence. CRITICAL: escape every double-quote "
    "that appears inside a string value as \\\" (or just use single quotes in your prose), so the "
    "JSON always parses. The object shape is exactly: "
    '{"overall_score": <int 0-100>, "breakdown": {"google": <int>, "reviews": <int>, '
    '"website": <int>, "aio": <int>}, "narrative": {"headline": <str>, "vs_competitors": <str>, '
    '"aio_verdict": <str>}, "fixes": [{"priority": "high"|"medium"|"low", "title": <str>, '
    '"why": <str>, "service": <str>}]}.'
)


# --- structured output schema -------------------------------------------------

class Breakdown(BaseModel):
    google: int
    reviews: int
    website: int
    aio: int


class Narrative(BaseModel):
    headline: str
    vs_competitors: str
    aio_verdict: str


class Fix(BaseModel):
    priority: Literal["high", "medium", "low"]
    title: str
    why: str
    service: str


class AuditSynthesis(BaseModel):
    overall_score: int
    breakdown: Breakdown
    narrative: Narrative
    fixes: list[Fix]


SYSTEM = (
    "You are an expert local SEO/AIO consultant writing a visibility audit for a small-business "
    "owner who is NOT technical and is short on time. Your job: make the gaps feel real, urgent, "
    "and fixable, using the actual data — never generic filler. Lower scores mean worse visibility. "
    "Be honest: if they're doing well in an area, say so. Write the 'why' in plain, vivid language an "
    "owner understands — money and customers, not jargon. The reader should finish thinking 'I need "
    "this fixed.'\n"
    "Follow these rules exactly:\n"
    "0. NEVER invent numbers. Do not state dollar amounts, percentages, or statistics that are not "
    "present in the data (e.g. 'losing $10,000/year', '50% more calls', 'in the top 1%'). They are "
    "unverifiable and a legal/credibility risk. Use ONLY the real figures in the data — review "
    "counts, ratings, scores, mention rates — and otherwise speak qualitatively ('customers are "
    "calling competitors instead of you').\n"
    "1. HEADLINE: specific and loss-framed, naming the business and its actual situation. NEVER a "
    "generic title like 'Improve Your Online Visibility' or 'Needs to Boost Visibility', and no "
    "invented dollar figures.\n"
    "2. vs_competitors: cite the real competitor names and their actual review counts from the data "
    "(e.g. 'Clean Home has 778 reviews to your 13').\n"
    "3. aio_verdict: each assistant was asked several times across different phrasings, so visibility "
    "is an appearance RATE, not yes/no. Use the real rate ('ChatGPT named you in 2 of 6 questions; "
    "Perplexity never did') — these come from the data and ARE allowed numbers. Name the SPECIFIC "
    "competitors the AI recommended INSTEAD (from competitors_mentioned). Because the sample is small, "
    "frame it as a snapshot, never a guarantee or a permanent state. If the business has no specific "
    "category or wasn't found on Google, say so plainly. IMPORTANT: if an engine's stability is "
    "'not_served' (served=false), Google simply did NOT show an AI Overview for this query — it answers "
    "with the local map pack. That is NOT an absence; if local_pack_rank is set, say they rank #N in "
    "Google's local results. Never describe a not_served engine as 'you didn't show up.'\n"
    "3b. If summary.organic_rank is set, that's the business's classic Google (blue-link) rank for the "
    "main query. AI Overviews rarely cite sites outside the top results, so when the business isn't "
    "being named by the assistants, a weak organic_rank (or its absence from organic_top) is the most "
    "likely upstream CAUSE — say so, and frame fixing organic ranking as the lever. Don't invent a rank "
    "if it's null.\n"
    "4. Each fix: 'title' is a short punchy ACTION (e.g. 'Close the review gap — 13 vs 700'); "
    "'service' is the done-for-you service name (e.g. 'Google Profile optimization', 'review "
    "generation', 'AI-citation content', 'local landing pages'). NEVER put the service name in the "
    "title. Give 5-8 fixes ordered high→low priority."
)


def _summarize_for_prompt(audit: dict[str, Any]) -> dict[str, Any]:
    """Trim the audit to the facts Claude needs (drop long raw AI answers)."""
    aio = audit.get("aio", {})
    engines = {
        k: {
            "recommended": v.get("recommended"),          # named in >=1 sample (the real win)
            "recommended_rate": v.get("recommended_rate"),  # appearance RATE across samples (the honest number)
            "named_in": (f"{v.get('recommended_count')}/{v.get('samples')}"
                         if v.get("samples") else None),  # e.g. "2/6 runs"
            "stability": v.get("stability"),              # consistent | intermittent | absent | not_served
            "served": v.get("served"),                    # False = engine gave no AI answer to appear in
            "local_pack_rank": v.get("local_pack_rank"),  # rank in Google's local map pack (when no overview)
            "organic_rank": v.get("organic_rank"),        # classic blue-link rank — drives whether AI cites them
            "cited_as_source": v.get("cited_as_source"),  # domain in sources, but maybe not recommended
            "mention_snippet": v.get("mention_snippet"),
            "competitors_mentioned": v.get("competitors_mentioned"),
            "cited_domains": v.get("cited_domains"),
            "present": v.get("present"),
            "error": v.get("error"),
        }
        for k, v in (aio.get("engines") or {}).items()
    }
    return {
        "business": {k: audit["business"].get(k) for k in ("query_name", "resolved_name", "primary_category", "address")},
        "google_presence": audit.get("google_presence"),
        "reviews": {k: audit["reviews"].get(k) for k in ("rating", "count", "responds_to_reviews")},
        "competitors": audit.get("competitors"),
        "website": {k: audit["website"].get(k) for k in (
            "url", "reachable", "analyzable", "http_status", "pagespeed", "has_localbusiness_schema",
            "has_faq_schema", "schema_types", "service_pages_found", "location_pages_found", "gaps")},
        "aio": {"queries": aio.get("queries"), "engines": engines, "summary": aio.get("summary")},
        "errors": audit.get("errors"),
    }


def _user_prompt(audit: dict[str, Any]) -> str:
    payload = json.dumps(_summarize_for_prompt(audit), ensure_ascii=False, indent=2)
    return (
        "Here is the audit data for one local business. Produce the visibility score (0-100, lower "
        "= less visible), a per-area breakdown, a punchy owner-facing narrative, and 5-8 prioritized "
        "fixes. Use the real numbers and the AI-assistant results.\n\n```json\n" + payload + "\n```"
    )


def _repair_json(t: str) -> str:
    """Best-effort fixes for the common ways a model's JSON is *almost* valid."""
    t = (t.replace("“", '"').replace("”", '"')   # smart double quotes
          .replace("‘", "'").replace("’", "'"))  # smart single quotes
    t = re.sub(r",\s*([}\]])", r"\1", t)                    # trailing commas
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", t)     # stray control chars
    return t


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the JSON object out of a model's text response (tolerates fences/prose/smart-quotes)."""
    t = (text or "").strip()
    # Prefer a fenced ```json ... ``` block if present (what we ask claude_cli/groq to produce).
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.DOTALL)
    if m:
        t = m.group(1)
    else:
        if t.startswith("```"):
            t = t[3:]
            if t[:4].lower() == "json":
                t = t[4:]
        i, j = t.find("{"), t.rfind("}")
        if i == -1 or j == -1:
            raise ValueError("no JSON object found in synthesis output")
        t = t[i:j + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return json.loads(_repair_json(t))


def _apply(audit: dict[str, Any], result: AuditSynthesis) -> None:
    audit["score"] = {
        "overall": max(0, min(100, result.overall_score)),
        "breakdown": result.breakdown.model_dump(),
        "rubric_notes": "AI synthesis",
    }
    audit["narrative"] = result.narrative.model_dump()
    audit["fixes"] = [f.model_dump() for f in result.fixes]


def _claude_synthesis(audit: dict[str, Any], key: str) -> None:
    """Anthropic API backend — structured output + adaptive thinking (~$0.06/audit)."""
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.parse(
        model=MODEL, max_tokens=12000, thinking={"type": "adaptive"},
        system=SYSTEM, messages=[{"role": "user", "content": _user_prompt(audit)}],
        output_format=AuditSynthesis,
    )
    _apply(audit, resp.parsed_output)
    u = resp.usage
    cost = (u.input_tokens / 1e6) * PRICE_IN.get(MODEL, 5.0) + (u.output_tokens / 1e6) * PRICE_OUT.get(MODEL, 25.0)
    common.add_cost(audit, "anthropic_synthesis", cost, MODEL)


def _claude_cli_synthesis(audit: dict[str, Any]) -> None:
    """Run synthesis via the local `claude` CLI (uses your Claude Code subscription).
    Output shape is enforced by the CLI's --json-schema flag (Claude Code >= 2.1.x): the envelope
    carries a validated, already-parsed `structured_output` object, so no fence/prose parsing is
    needed. `_extract_json(result)` remains as a fallback, plus ONE retry that feeds the validation
    error back — this replaces the old prompt-begging that produced unparseable JSON.
    NOTE: each call pulls in Claude Code's large system prompt, so the reported equivalent cost is
    high (~$0.30). It only SAVES money on a flat subscription (then it spends quota, not dollars);
    on metered billing the Anthropic backend is cheaper."""
    import subprocess
    schema = json.dumps(AuditSynthesis.model_json_schema())
    prompt = f"{SYSTEM}\n\n{_user_prompt(audit)}"
    last_err: Exception | None = None
    for _attempt in range(2):
        p = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json", "--json-schema", schema],
            capture_output=True, text=True, timeout=240,
        )
        if p.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {(p.stderr or p.stdout)[:200]}")
        env = json.loads(p.stdout)
        try:
            data = env.get("structured_output") or _extract_json(env.get("result", ""))
            _apply(audit, AuditSynthesis.model_validate(data))
            common.add_cost(audit, "claude_cli", float(env.get("total_cost_usd") or 0.0),
                            "Claude Code subscription (equiv.)")
            return
        except Exception as e:  # noqa: BLE001 - one retry with the error fed back
            last_err = e
            prompt = (f"{SYSTEM}\n\n{_user_prompt(audit)}\n\nYour previous answer failed validation "
                      f"({type(e).__name__}: {e}). Return ONLY a corrected JSON object matching the "
                      f"required schema.")
    raise RuntimeError(f"claude CLI synthesis failed after retry: {last_err}")


def _groq_synthesis(audit: dict[str, Any], key: str) -> None:
    """Groq free-tier backend (open models) — genuinely free within Groq's limits."""
    from groq import Groq
    client = Groq(api_key=key)
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": _user_prompt(audit) + "\n\n" + JSON_HINT}],
        response_format={"type": "json_object"}, temperature=0.4, max_tokens=4000,
    )
    _apply(audit, AuditSynthesis.model_validate(_extract_json(resp.choices[0].message.content)))
    common.add_cost(audit, "groq", 0.0, f"{GROQ_MODEL} (free tier)")


def _heuristic(audit: dict[str, Any]) -> None:
    """Transparent fallback score when Claude is unavailable — keeps the report renderable."""
    gp = audit.get("google_presence", {})
    google = gp.get("completeness_score") or 0

    rating = (audit.get("reviews") or {}).get("rating") or 0
    count = (audit.get("reviews") or {}).get("count") or 0
    comp_counts = [c.get("review_count") or 0 for c in (audit.get("competitors") or [])]
    top_comp = max(comp_counts) if comp_counts else 0
    review_ratio = min(1.0, (count / top_comp)) if top_comp else (0.5 if count else 0.0)
    reviews = round((min(rating, 5) / 5 * 50) + review_ratio * 50)

    web = audit.get("website") or {}
    if not web.get("url"):
        website = 0                       # genuinely no website — a real, scoreable gap
    elif web.get("analyzable") is False:
        website = None                    # we couldn't read it (bot-block) — unknown, exclude from score
    else:
        gaps = len(web.get("gaps") or [])
        website = 100 if (web.get("reachable") and gaps == 0) else max(0, 100 - gaps * 15)

    summary = (audit.get("aio") or {}).get("summary", {})
    checked = summary.get("engines_checked") or 0
    avg_rate = summary.get("recommended_rate_avg")
    if avg_rate is not None and summary.get("samples_per_engine"):
        aio = round(avg_rate * 100)                       # multi-sample: use the appearance RATE
    elif checked:
        aio = round((summary.get("mentioned_in", 0) / checked) * 100)
    else:
        aio = 0

    # Weighted average over the areas we could actually measure (drop website if it was unreadable).
    parts = [(google, 0.30), (reviews, 0.25), (website, 0.20), (aio, 0.25)]
    avail = [(v, w) for v, w in parts if v is not None]
    tw = sum(w for _, w in avail)
    overall = round(sum(v * w for v, w in avail) / tw) if tw else 0

    fixes = []
    if web.get("analyzable") is not False:    # don't turn "we couldn't read your site" into a fake fix
        for g in (web.get("gaps") or [])[:4]:
            fixes.append({"priority": "high", "title": g.split(" — ")[0], "why": g,
                          "service": "Website & schema optimization"})
    if aio < 50:
        fixes.append({"priority": "high",
                      "title": "You're invisible to AI assistants",
                      "why": "When people ask ChatGPT/Google's AI for the best in your area, you don't come up.",
                      "service": "AI-citation content + directory presence"})
    for m in (gp.get("missing") or [])[:3]:
        fixes.append({"priority": "medium", "title": f"Google profile missing: {m}",
                      "why": f"Your Google Business Profile is missing {m}, which hurts ranking and trust.",
                      "service": "Google Profile optimization"})

    audit["score"] = {"overall": overall,
                      "breakdown": {"google": google, "reviews": reviews, "website": website, "aio": aio},
                      "rubric_notes": "heuristic fallback (Claude synthesis unavailable)"}
    spe = summary.get("samples_per_engine")
    lp_rank = summary.get("local_pack_rank")
    if spe:
        aio_verdict = (f"Across {spe} questions per assistant, you were named about {aio}% of the time"
                       f" ({summary.get('mentioned_in', 0)} of {checked} assistants mentioned you at "
                       f"least once). Small sample — treat as a snapshot, not a guarantee.")
        if summary.get("google_ai_served") is False:
            aio_verdict += (f" Google showed no AI Overview for this search — it answers with the local "
                            f"map pack" + (f", where you rank #{lp_rank}." if lp_rank else "."))
        org = summary.get("organic_rank")
        if org and org > 3:
            aio_verdict += (f" You sit around #{org} in Google's classic results, and assistants rarely "
                            f"cite past the top few — lifting that ranking is the lever.")
        elif not org and summary.get("organic_top") and not summary.get("mentioned_in"):
            aio_verdict += (" You don't appear in Google's top organic results at all — the likeliest "
                            "reason the assistants aren't citing you.")
    elif checked:
        aio_verdict = f"You appeared in {summary.get('mentioned_in', 0)} of {checked} AI assistants we asked."
    else:
        aio_verdict = "AI-assistant visibility was not checked (add the AI keys)."
    audit["narrative"] = {
        "headline": f"Your visibility score is {overall}/100 — here's where you're losing customers.",
        "vs_competitors": (f"Competitors near you have up to {top_comp} reviews; you have {count}."
                           if top_comp else "We compared you to nearby competitors."),
        "aio_verdict": aio_verdict,
    }
    audit["fixes"] = fixes


def enrich(audit: dict[str, Any]) -> dict[str, Any]:
    """Synthesize via the configured backend; always falls back to the heuristic so a run never fails."""
    try:
        if BACKEND == "heuristic":
            _heuristic(audit)
            return audit
        if BACKEND == "groq":
            _groq_synthesis(audit, common.get_key("groq", required=True))
            return audit
        if BACKEND == "claude_cli":
            _claude_cli_synthesis(audit)
            return audit
        # default: anthropic API
        key = common.get_key("anthropic")
        if not key:
            common.record_error(audit, "score", "ANTHROPIC_API_KEY not set — used heuristic score.")
            _heuristic(audit)
            return audit
        _claude_synthesis(audit, key)
    except Exception as e:  # noqa: BLE001 - never let synthesis crash the run
        common.record_error(audit, "score", f"Synthesis backend '{BACKEND}' failed ({type(e).__name__}: {e}); used heuristic.")
        _heuristic(audit)
    return audit


def main() -> None:
    ap = argparse.ArgumentParser(description="Synthesize an audit into score + narrative + fixes.")
    ap.add_argument("--from", dest="src", required=True, help="path to an audit.json")
    args = ap.parse_args()

    audit = common.load_json(args.src)
    enrich(audit)
    import json
    print(json.dumps({"score": audit["score"], "narrative": audit["narrative"],
                      "fixes": audit["fixes"]}, indent=2, ensure_ascii=False))
    if audit["errors"]:
        print("Errors:", audit["errors"])


if __name__ == "__main__":
    main()
