"""The 2026-07-12 pipeline reweight: AI-default-cited directories (Bing/Facebook/Yelp — the
ChatGPT-via-Bing pipeline) must outrank filler directories even when no engine happened to cite
them in the sampled answers."""

from tools.directory_gap import build_gap_list, _pipeline_bonus


def _bare_audit():
    return {
        "business": {"phone": "555-0100", "website": None},
        "aio": {"queries": ["best brake repair in tempe"], "engines": {}},
    }


def _scores(section_rows):
    return {r["platform"]: r["score"] for r in section_rows}


def test_pipeline_bonus_counts_default_engines():
    assert _pipeline_bonus({"cited_by_default": ["chatgpt", "perplexity"]}) == 4
    assert _pipeline_bonus({"cited_by_default": []}) == 0
    assert _pipeline_bonus({}) == 0


def test_uncited_ordering_favors_ai_pipeline():
    gap = build_gap_list(_bare_audit())
    s = _scores(gap["sections"]["claim_free_first"])
    # Yelp (all 3 engines) and GBP lead; Bing (ChatGPT pipeline) beats Apple (no default citations)
    assert s["Yelp"] == max(s.values())
    assert s["Bing Places"] > s["Apple Business Connect"]
    assert s["Facebook Business Page"] > s["Bing Places"]  # chatgpt+perplexity default
