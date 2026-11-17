"""New-business path — a market landscape scan (there's no subject to self-audit).

An existing business gets the full visibility audit (tools/run_audit). A brand-new
business has nothing to audit, so we scan the MARKET it's about to enter: who already
owns Google's local results, who the AI assistants already recommend, and which domains
those assistants trust — the exact places to get listed on day one.

Maximum reuse of the existing engine against the same JSON contract:
  - fetch_gbp._text_search : "{category} in {city}" -> the market's players
  - check_aio.enrich       : who ChatGPT/Perplexity/Google-AI recommend + cited domains.
    Works UNCHANGED because a landscape IS a common.new_audit scaffold with competitors
    pre-filled — check_aio cross-references who the AIs name against that player list.

ponytail: the deterministic market read (saturation, top players, AI picks, listing
targets) is the deliverable. A capable-model GREEN/YELLOW/RED viability verdict — which
needs the interview's price/cost levers — is a documented extension, not built here.

CLI:  python -m tools.run_landscape --category "pilates studio" --city "Mesa, AZ"
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from tools import common, fetch_gbp, check_aio

ESTABLISHED_REVIEWS = 100   # ponytail: "established competitor" cutoff; tune against real markets


def _market_search(landscape: dict[str, Any], category: str, city: str) -> None:
    key = common.get_key("google_maps")
    if not key:
        common.record_error(landscape, "run_landscape",
                            "GOOGLE_MAPS_API_KEY not set — skipped market search.")
        return
    try:
        # Reuse the sibling module's Places primitive rather than re-implementing the call.
        places = fetch_gbp._text_search(f"{category} in {city}", key,
                                        fetch_gbp.COMPETITOR_MASK, max_results=10)
        common.add_cost(landscape, "places_text_search", fetch_gbp.COST_TEXT_SEARCH, "market/competitors")
    except Exception as e:  # noqa: BLE001 — degrade like the rest of the engine
        common.record_error(landscape, "run_landscape", f"Market search error: {e}")
        return
    landscape["competitors"] = [
        {"name": fetch_gbp._name_of(c), "rating": c.get("rating"),
         "review_count": c.get("userRatingCount"), "place_id": c.get("id")}
        for c in places
    ][:10]


def _summarize(landscape: dict[str, Any]) -> None:
    players = landscape.get("competitors", []) or []
    established = [p for p in players if (p.get("review_count") or 0) >= ESTABLISHED_REVIEWS]
    top = sorted(players, key=lambda p: (p.get("review_count") or 0), reverse=True)[:5]

    aio = landscape.get("aio", {}) or {}
    rec: dict[str, int] = {}
    for e in (aio.get("engines") or {}).values():
        for cname in (e.get("competitors_mentioned") or []):
            rec[cname] = rec.get(cname, 0) + 1
    ai_recommends = [k for k, _ in sorted(rec.items(), key=lambda kv: (-kv[1], kv[0]))][:5]
    trusted = (aio.get("summary") or {}).get("all_cited_domains", [])

    landscape["market"] = {
        "player_count": len(players),
        "established_count": len(established),
        "top_players": top,
        "ai_recommends": ai_recommends,       # who the assistants already name in this market
        "trusted_domains": trusted,           # where a newcomer should get listed from day one
        "saturation": ("high" if len(established) >= 5
                       else "moderate" if len(established) >= 2 else "low"),
    }


def run(category: str, city: str, name: str | None = None, website: str | None = None,
        outdir: Path | None = None) -> dict:
    # A landscape IS an audit scaffold (so check_aio works unchanged), tagged mode=landscape.
    landscape = common.new_audit(name or category, city, website)
    landscape["meta"]["mode"] = "landscape"
    landscape["business"]["primary_category"] = category
    landscape["business"]["search_category"] = category

    print("[1/3] Market players (Google)…")
    _market_search(landscape, category, city)
    print("[2/3] AI-assistant recommendations for this market…")
    check_aio.enrich(landscape, category=category)
    print("[3/3] Market read…")
    _summarize(landscape)

    outdir = outdir or (common.TMP / common.slugify(f"landscape-{category}-{city}"))
    outdir.mkdir(parents=True, exist_ok=True)
    common.save_json(landscape, outdir / "landscape.json")

    mk = landscape["market"]
    print("\n" + "=" * 60)
    print(f"  Market: {category} in {city}")
    print(f"  Players found : {mk['player_count']} "
          f"(established: {mk['established_count']}, saturation: {mk['saturation']})")
    print(f"  AI recommends : {', '.join(mk['ai_recommends']) or '—'}")
    print(f"  Get listed on : {', '.join(mk['trusted_domains'][:6]) or '—'}")
    print(f"  Est. run cost : ${landscape['meta']['cost']['total_usd']}")
    print("=" * 60)
    return landscape


def main() -> None:
    ap = argparse.ArgumentParser(description="Scan the market a new business is entering.")
    ap.add_argument("--category", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--name", default=None, help="working name of the idea (optional)")
    ap.add_argument("--website", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    run(args.category, args.city, args.name, args.website, Path(args.out) if args.out else None)


if __name__ == "__main__":
    main()
