"""Phase 5a — render the audit JSON into a branded HTML report (the sales asset).

The renderer is pure presentation: it derives display values from the canonical `audit` dict and
feeds them to a self-contained Jinja2 template (`templates/report.html.j2`). No external fonts or
assets, so it opens anywhere and prints cleanly to PDF — and the future web front-end can reuse the
exact same template against the same JSON.

Includes the Google attribution required by the Places API ToS.

CLI:  python -m tools.render_report --from .tmp/<slug>/audit.json --out report.html
"""

from __future__ import annotations

import argparse
import os
import re
from datetime import datetime
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from tools import common

BRAND = os.environ.get("BRAND_NAME", "Local Visibility")

_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _clean_text(s: str | None) -> str | None:
    """Strip markdown link/bold noise from AI answers so report quotes read like plain prose."""
    if not s:
        return s
    s = _MD_LINK.sub(r"\1", s)                 # [label](url) -> label
    s = s.replace("**", "").replace("__", "")  # drop bold markers
    return re.sub(r"\s+", " ", s).strip()
TEMPLATES_DIR = common.ROOT / "templates"

ENGINE_LABELS = {
    "chatgpt": "ChatGPT",
    "perplexity": "Perplexity",
    "google_ai_overview": "Google AI Overview",
}


def _score_color(v: int | None) -> str:
    if v is None:
        return "#9aa0a6"
    if v < 40:
        return "#e2403b"   # red — alarming, motivates the fix
    if v < 70:
        return "#f5a623"   # amber
    return "#1faa59"       # green


def _score_label(v: int | None) -> str:
    if v is None:
        return "Not scored"
    if v < 40:
        return "Critically low"
    if v < 70:
        return "Needs work"
    return "Strong"


def _build_view(audit: dict[str, Any]) -> dict[str, Any]:
    score = (audit.get("score") or {}).get("overall")
    breakdown_raw = (audit.get("score") or {}).get("breakdown") or {}
    breakdown = [
        {"label": "Google profile", "value": breakdown_raw.get("google"), "color": _score_color(breakdown_raw.get("google"))},
        {"label": "Reviews", "value": breakdown_raw.get("reviews"), "color": _score_color(breakdown_raw.get("reviews"))},
        {"label": "Website", "value": breakdown_raw.get("website"), "color": _score_color(breakdown_raw.get("website"))},
        {"label": "AI assistants", "value": breakdown_raw.get("aio"), "color": _score_color(breakdown_raw.get("aio"))},
    ]

    biz = audit["business"]
    reviews = audit.get("reviews", {})
    my_reviews = reviews.get("count") or 0
    my_rating = reviews.get("rating")
    competitors = sorted(
        audit.get("competitors", []),
        key=lambda c: (c.get("review_count") or 0),
        reverse=True,
    )[:5]

    aio = audit.get("aio", {})
    engines = []
    for key, label in ENGINE_LABELS.items():
        e = (aio.get("engines") or {}).get(key)
        if not e:
            continue
        answer = _clean_text((e.get("raw_answer") or "").strip()) or ""
        engines.append({
            "label": label,
            "asked": e.get("asked"),
            "mentioned": e.get("mentioned"),
            "recommended": e.get("recommended"),
            # multi-sample: appearance RATE across re-asked prompts (None on old single-sample audits)
            "samples": e.get("samples"),
            "recommended_count": e.get("recommended_count"),
            "recommended_rate": e.get("recommended_rate"),
            "named_in": (f"{e.get('recommended_count')} of {e.get('samples')}"
                         if e.get("samples") else None),
            "rate_pct": (round((e.get("recommended_rate") or 0) * 100)
                         if e.get("samples") else None),
            "stability": e.get("stability"),
            "served": e.get("served"),
            "local_pack_rank": e.get("local_pack_rank"),
            "local_pack_top": e.get("local_pack_top") or [],
            "organic_rank": e.get("organic_rank"),
            "organic_match": e.get("organic_match"),
            "cited_as_source": e.get("cited_as_source"),
            "mention_snippet": _clean_text(e.get("mention_snippet")),
            "present": e.get("present", True),
            "cited_domains": e.get("cited_domains") or [],
            "competitors_mentioned": e.get("competitors_mentioned") or [],
            "answer": (answer[:360] + "…") if len(answer) > 360 else answer,
            "error": e.get("error"),
        })
    aio_summary = aio.get("summary", {})
    # The heading should echo the question actually asked ("best brake repair in …"), not Google's
    # clunky primary category ("Car repair and maintenance service").
    aio_q = (aio.get("queries") or [""])[0] or ""
    m = re.match(r"\s*best\s+(.+?)\s+in\s+", aio_q, re.I)
    aio_category = m.group(1) if m else biz.get("primary_category")

    fixes = sorted(
        audit.get("fixes", []),
        key=lambda f: {"high": 0, "medium": 1, "low": 2}.get(f.get("priority"), 3),
    )

    generated = audit.get("meta", {}).get("generated_at")
    try:
        gen_dt = datetime.fromisoformat(generated.replace("Z", "+00:00")) if generated else None
        generated_fmt = gen_dt.strftime("%B %-d, %Y") if gen_dt else ""
    except Exception:
        generated_fmt = generated or ""

    return {
        "brand": BRAND,
        "business_name": biz.get("resolved_name") or biz.get("query_name"),
        "category": biz.get("primary_category"),
        "aio_category": aio_category,
        "city": audit.get("meta", {}).get("query", {}).get("city"),
        "address": biz.get("address"),
        "generated": generated_fmt,
        "score": score,
        "score_color": _score_color(score),
        "score_label": _score_label(score),
        "breakdown": breakdown,
        "google": audit.get("google_presence", {}),
        "my_rating": my_rating,
        "my_reviews": my_reviews,
        "competitors": competitors,
        "website": audit.get("website", {}),
        "engines": engines,
        "aio_summary": aio_summary,
        "aio_queries": aio.get("queries", []),
        "all_cited_domains": aio_summary.get("all_cited_domains", []),
        "narrative": audit.get("narrative") or {},
        "fixes": fixes,
        "errors": audit.get("errors", []),
        "cost": audit.get("meta", {}).get("cost", {}).get("total_usd"),
    }


def _env() -> Environment:
    # The templates end in .j2, so autoescape must include "j2" (and default_for_string) or it
    # silently stays OFF, letting business names / AI answers inject HTML into the report.
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html", "xml", "j2"), default_for_string=True),
    )


def render(audit: dict[str, Any]) -> str:
    return _env().get_template("report.html.j2").render(v=_build_view(audit))


def _build_landscape_view(ls: dict[str, Any]) -> dict[str, Any]:
    """Display view for a new-business market scan (tools/run_landscape) — no subject to score."""
    mk = ls.get("market", {}) or {}
    generated = ls.get("meta", {}).get("generated_at")
    try:
        gen_dt = datetime.fromisoformat(generated.replace("Z", "+00:00")) if generated else None
        generated_fmt = gen_dt.strftime("%B %-d, %Y") if gen_dt else ""
    except Exception:
        generated_fmt = generated or ""
    return {
        "brand": BRAND,
        "category": ls["business"].get("primary_category") or ls["business"].get("search_category"),
        "city": ls.get("meta", {}).get("query", {}).get("city"),
        "generated": generated_fmt,
        "saturation": mk.get("saturation"),
        "player_count": mk.get("player_count") or 0,
        "established_count": mk.get("established_count") or 0,
        "top_players": mk.get("top_players", []),
        "ai_recommends": mk.get("ai_recommends", []),
        "trusted_domains": mk.get("trusted_domains", []),
        "errors": ls.get("errors", []),
        "cost": ls.get("meta", {}).get("cost", {}).get("total_usd"),
    }


def render_landscape(ls: dict[str, Any]) -> str:
    return _env().get_template("landscape.html.j2").render(v=_build_landscape_view(ls))


def main() -> None:
    ap = argparse.ArgumentParser(description="Render an audit.json into a branded HTML report.")
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    audit = common.load_json(args.src)
    html = render(audit)
    out = args.out or str(common.TMP / f"{common.slugify(audit['business'].get('query_name','audit'))}-report.html")
    from pathlib import Path
    Path(out).write_text(html, encoding="utf-8")
    print(f"Report written: {out}")


if __name__ == "__main__":
    main()
