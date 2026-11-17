"""Offline unit tests for the two new audit signals (no network / paid calls):
  - analyze_site._ai_crawler_access  -> robots.txt AI-crawler block detection
  - check_aio._organic_rank          -> classic organic (blue-link) rank capture
Run:  python3 -m pytest tests/test_new_signals.py -q   (or: python3 tests/test_new_signals.py)
"""
from __future__ import annotations

from tools import analyze_site as A
from tools import check_aio as C

ROOT = "https://joesbrakes.com"
NAME = "Joe's Mobile Brake Repair"
SITE = "https://joesbrakes.com/"


# --- robots.txt AI-crawler access -------------------------------------------------

def test_no_robots_means_all_allowed():
    r = A._ai_crawler_access(None, ROOT)
    assert r["checked"] is False
    assert r["blocked"] == []
    assert "GPTBot" in r["allowed"] and len(r["allowed"]) == len(A._AI_CRAWLERS)


def test_blocks_specific_ai_bots():
    robots = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: PerplexityBot\nDisallow: /\n"
    r = A._ai_crawler_access(robots, ROOT)
    assert r["checked"] is True
    assert "GPTBot" in r["blocked"] and "PerplexityBot" in r["blocked"]
    assert "ClaudeBot" in r["allowed"]      # not named -> falls under (absent) default = allowed


def test_open_robots_blocks_nothing():
    robots = "User-agent: *\nDisallow:\nSitemap: https://joesbrakes.com/sitemap.xml\n"
    r = A._ai_crawler_access(robots, ROOT)
    assert r["blocked"] == []


def test_sitewide_disallow_blocks_all_ai_bots():
    robots = "User-agent: *\nDisallow: /\n"
    r = A._ai_crawler_access(robots, ROOT)
    assert sorted(r["blocked"]) == sorted(b for b, _ in A._AI_CRAWLERS)
    assert r["allowed"] == []


# --- organic (blue-link) rank -----------------------------------------------------

def test_own_domain_ranks():
    data = {"organic_results": [
        {"position": 1, "title": "Brake Masters - Tempe", "link": "https://brakemasters.com/tempe"},
        {"position": 2, "title": "Best Brake Repair | Yelp", "link": "https://www.yelp.com/search?x"},
        {"position": 3, "title": "Joe's Mobile Brake Repair", "link": "https://joesbrakes.com/"},
    ]}
    rank, match, top = C._organic_rank(data, NAME, SITE)
    assert rank == 3 and match == "domain"
    assert top[:3] == ["brakemasters.com", "yelp.com", "joesbrakes.com"]


def test_named_in_directory_title_when_own_site_absent():
    data = {"organic_results": [
        {"position": 1, "title": "Top 10 brake shops incl Joe's Mobile Brake Repair", "link": "https://yelp.com/x"},
        {"position": 2, "title": "Brake Masters", "link": "https://brakemasters.com"},
    ]}
    rank, match, top = C._organic_rank(data, NAME, SITE)
    assert rank == 1 and match == "name"


def test_absent_from_organic():
    data = {"organic_results": [
        {"position": 1, "title": "Brake Masters", "link": "https://brakemasters.com"},
        {"position": 2, "title": "Pep Boys Tempe", "link": "https://pepboys.com"},
    ]}
    rank, match, top = C._organic_rank(data, NAME, SITE)
    assert rank is None and match is None
    assert top == ["brakemasters.com", "pepboys.com"]


def test_domain_match_ignores_www_prefix():
    data = {"organic_results": [
        {"position": 4, "title": "Home", "link": "https://www.joesbrakes.com/services"},
    ]}
    rank, match, _ = C._organic_rank(data, NAME, SITE)
    assert rank == 4 and match == "domain"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"\nALL {len(fns)} NEW-SIGNAL TESTS PASSED ✓")
