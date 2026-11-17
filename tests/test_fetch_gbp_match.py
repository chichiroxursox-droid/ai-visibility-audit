"""Regression test for the 'no Google profile -> wrong business' bug.

A Places *text* search is fuzzy: querying a business with no Google Business Profile still returns the
nearest relevance match. The audit must NOT adopt that stranger's profile. These tests pin the
name-similarity guard that decides "is this actually the business we searched for?".

Surfaced by auditing "Main Line Basketball Lessons" (no GBP), which Google resolved to the unrelated
"Next Level Sports" — contaminating the entire report.
"""

from tools.fetch_gbp import _name_match_score, NAME_MATCH_THRESHOLD


def _is_match(query: str, place: str) -> bool:
    return _name_match_score(query, place) >= NAME_MATCH_THRESHOLD


# --- the actual failure that motivated this guard ---

def test_unrelated_business_is_rejected():
    # The real-world bug: these must NOT be treated as the same business.
    assert not _is_match("Main Line Basketball Lessons", "Next Level Sports")


def test_other_nearby_noise_rejected():
    assert not _is_match("Main Line Basketball Lessons", "The Junior League of Philadelphia, Inc.")
    assert not _is_match("Main Line Basketball Lessons", "PALM Center for Positive Aging")


# --- legitimate matches must still pass (no false negatives) ---

def test_exact_name_matches():
    assert _is_match("Joe's Plumbing", "Joe's Plumbing")


def test_minor_suffix_variations_match():
    assert _is_match("Joe's Plumbing", "Joe's Plumbing LLC")
    assert _is_match("Joe's Mobile Brake Repair", "Joe's Mobile Brake Repair")


def test_distinctive_tokens_match_despite_generic_words():
    # "lessons"/"academy" are generic; the distinctive "main line basketball" still ties them together.
    assert _is_match("Main Line Basketball Lessons", "Main Line Basketball Academy")


# --- score is a sane 0..1 confidence ---

def test_score_bounds_and_ordering():
    s_good = _name_match_score("Joe's Plumbing", "Joe's Plumbing LLC")
    s_bad = _name_match_score("Main Line Basketball Lessons", "Next Level Sports")
    assert 0.0 <= s_bad < s_good <= 1.0


def test_empty_inputs_score_zero():
    assert _name_match_score("", "Anything") == 0.0
    assert _name_match_score("Something", "") == 0.0
