"""Runnable check for the new-business landscape scan — NO paid APIs.

Fakes the Google market search and runs with the AI keys unset (check_aio then
skips the paid calls entirely), so orchestration + the market read are verified free.

    python -m tools.test_landscape
"""
from __future__ import annotations

import os

from tools import common, fetch_gbp, run_landscape


def demo() -> None:
    # No AI keys => check_aio makes zero paid calls; dummy Google key so the (faked) search proceeds.
    for var in ("OPENAI_API_KEY", "PERPLEXITY_API_KEY", "SERPAPI_KEY"):
        os.environ.pop(var, None)
    os.environ["GOOGLE_MAPS_API_KEY"] = "test-dummy"

    fake_places = [
        {"id": "p1", "displayName": {"text": "Iron Core Pilates"}, "rating": 4.9, "userRatingCount": 320},
        {"id": "p2", "displayName": {"text": "Riverside Pilates"}, "rating": 4.8, "userRatingCount": 210},
        {"id": "p3", "displayName": {"text": "Bend Studio"}, "rating": 4.7, "userRatingCount": 40},
    ]
    orig = fetch_gbp._text_search
    fetch_gbp._text_search = lambda q, key, mask, max_results=10: fake_places
    try:
        ls = run_landscape.run("pilates studio", "Mesa, AZ", outdir=common.TMP / "test-landscape")
    finally:
        fetch_gbp._text_search = orig

    assert ls["meta"]["mode"] == "landscape", ls["meta"]
    mk = ls["market"]
    assert mk["player_count"] == 3, mk
    assert mk["established_count"] == 2, mk            # 320 & 210 clear 100; 40 doesn't
    assert mk["saturation"] == "moderate", mk
    assert mk["top_players"][0]["name"] == "Iron Core Pilates", mk
    # AIO ran subject-less with no keys -> nothing checked, nothing spent beyond the one search.
    assert (ls["aio"]["summary"]["engines_checked"] or 0) == 0, ls["aio"]["summary"]
    assert ls["meta"]["cost"]["total_usd"] == round(fetch_gbp.COST_TEXT_SEARCH, 4), ls["meta"]["cost"]

    print("landscape OK: market search + read + subject-less AIO; saturation=moderate, cost=1 search")


if __name__ == "__main__":
    demo()
