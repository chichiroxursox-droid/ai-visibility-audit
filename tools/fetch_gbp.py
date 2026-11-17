"""Phase 1 — Google Business Profile + competitors via the Places API (New).

Fills the `business`, `google_presence`, `reviews`, and `competitors` sections of the audit.

Uses the *New* Places API (https://places.googleapis.com/v1/...), which REQUIRES a field mask
header on every call (the mask also determines billing SKU). We:
  1. Text Search to resolve "<name> <city>" -> the place + its id.
  2. Place Details for the rich profile (hours, photos, reviews, website, phone, categories).
  3. Text Search "<category> in <city>" for ranked competitors.

ToS note: when this data is displayed in a report we must show Google attribution (the renderer
does), and we must not persist it long-term — audits are regenerated fresh.

CLI:  python -m tools.fetch_gbp --name "Joe's Plumbing" --city "Austin, TX"
"""

from __future__ import annotations

import argparse
import re
from difflib import SequenceMatcher
from typing import Any

import requests

from tools import common

PLACES_BASE = "https://places.googleapis.com/v1"

# Rough per-call USD estimates (Advanced/Atmosphere tier — confidence: medium; confirm in console).
COST_TEXT_SEARCH = 0.017
COST_PLACE_DETAILS = 0.025

RESOLVE_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.rating,"
    "places.userRatingCount,places.primaryType,places.primaryTypeDisplayName,"
    "places.types,places.businessStatus"
)
DETAILS_MASK = (
    "id,displayName,formattedAddress,nationalPhoneNumber,websiteUri,rating,userRatingCount,"
    "regularOpeningHours,currentOpeningHours,primaryType,primaryTypeDisplayName,types,"
    "businessStatus,photos,reviews,googleMapsUri"
)
COMPETITOR_MASK = (
    "places.id,places.displayName,places.rating,places.userRatingCount,"
    "places.primaryTypeDisplayName"
)


def _headers(key: str, field_mask: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": field_mask,
    }


def _text_search(query: str, key: str, field_mask: str, max_results: int = 10) -> list[dict[str, Any]]:
    resp = requests.post(
        f"{PLACES_BASE}/places:searchText",
        headers=_headers(key, field_mask),
        json={"textQuery": query, "maxResultCount": max_results, "languageCode": "en"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("places", [])


def _place_details(place_id: str, key: str, field_mask: str) -> dict[str, Any]:
    resp = requests.get(
        f"{PLACES_BASE}/places/{place_id}",
        headers=_headers(key, field_mask),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _name_of(place: dict[str, Any]) -> str | None:
    dn = place.get("displayName")
    if isinstance(dn, dict):
        return dn.get("text")
    return dn


def _category_of(place: dict[str, Any]) -> str | None:
    disp = place.get("primaryTypeDisplayName")
    if isinstance(disp, dict) and disp.get("text"):
        return disp["text"]
    pt = place.get("primaryType")
    if pt:
        return pt.replace("_", " ").title()
    return None


# A Places *text* search is fuzzy: querying a business that has no Google Business Profile still
# returns the nearest relevance match (searching "Main Line Basketball Lessons" surfaced an unrelated
# "Next Level Sports"). Adopting results[0] blindly builds the whole report around the wrong company,
# so we gate on a name-similarity check. Below threshold = "this isn't them" = the report's finding.
_GENERIC_NAME_WORDS = {
    "the", "and", "of", "a", "llc", "inc", "co", "corp", "company", "ltd",
    "lessons", "lesson", "training", "trainer", "coach", "coaching", "academy", "club",
    "services", "service", "studio", "center", "centre", "group", "sports", "sport",
}
NAME_MATCH_THRESHOLD = 0.45


def _norm_name(s: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def _name_match_score(query_name: str | None, place_name: str | None) -> float:
    """Confidence (0..1) that `place_name` is the business the user searched for.

    Takes the stronger of (a) distinctive-token overlap — how many of the query's *non-generic* words
    appear in the place name — and (b) a fuzzy whole-string ratio. Generic words like "lessons" or
    "services" are ignored so they can't manufacture a match between two unrelated names.
    """
    q, p = _norm_name(query_name), _norm_name(place_name)
    if not q or not p:
        return 0.0
    distinctive = [w for w in q.split() if w not in _GENERIC_NAME_WORDS]
    ptokens = set(p.split())
    overlap = (sum(1 for w in distinctive if w in ptokens) / len(distinctive)) if distinctive else 0.0
    ratio = SequenceMatcher(None, q, p).ratio()
    return max(overlap, ratio)


def _completeness(details: dict[str, Any]) -> tuple[int, list[str]]:
    """Heuristic 0-100 profile completeness + the list of what's missing."""
    checks = [
        ("website", 15, bool(details.get("websiteUri"))),
        ("phone number", 15, bool(details.get("nationalPhoneNumber"))),
        ("opening hours", 15, bool(details.get("regularOpeningHours"))),
        ("5+ photos", 15, len(details.get("photos", []) or []) >= 5),
        ("a star rating", 10, details.get("rating") is not None),
        ("a business category", 10, bool(details.get("primaryType"))),
        ("10+ reviews", 10, (details.get("userRatingCount") or 0) >= 10),
        ("operational status", 10, details.get("businessStatus") == "OPERATIONAL"),
    ]
    score = sum(pts for _, pts, ok in checks if ok)
    missing = [label for label, _, ok in checks if not ok]
    return score, missing


def _map_reviews(details: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in details.get("reviews", []) or []:
        author = (r.get("authorAttribution") or {}).get("displayName")
        text = r.get("text")
        if isinstance(text, dict):
            text = text.get("text")
        out.append({
            "author": author,
            "rating": r.get("rating"),
            "text": text,
            "when": r.get("relativePublishingTimeDescription"),
        })
    return out


def enrich(audit: dict[str, Any]) -> dict[str, Any]:
    """Populate business/google_presence/reviews/competitors. Mutates and returns audit."""
    key = common.get_key("google_maps")
    name = audit["business"]["query_name"]
    city = audit["meta"]["query"]["city"]

    if not key:
        common.record_error(audit, "fetch_gbp", "GOOGLE_MAPS_API_KEY not set — skipped Google data.")
        return audit

    # 1. Resolve the business.
    try:
        results = _text_search(f"{name} {city}", key, RESOLVE_MASK, max_results=1)
        common.add_cost(audit, "places_text_search", COST_TEXT_SEARCH, "resolve business")
    except requests.HTTPError as e:
        common.record_error(audit, "fetch_gbp", f"Places text search failed: {e} :: {getattr(e.response,'text','')[:300]}")
        return audit
    except Exception as e:  # noqa: BLE001
        common.record_error(audit, "fetch_gbp", f"Places text search error: {e}")
        return audit

    if not results:
        common.record_error(
            audit, "fetch_gbp",
            f"'{name}' not found on Google for '{city}'. Finding: the business may have no Google "
            f"Business Profile or is not discoverable — a critical visibility gap.",
        )
        return audit

    place = results[0]

    # Guard against fuzzy mis-resolution: a text search always returns *something*, so confirm the
    # closest result is actually this business before adopting its profile, reviews and competitors.
    match = _name_match_score(name, _name_of(place))
    if match < NAME_MATCH_THRESHOLD:
        common.record_error(
            audit, "fetch_gbp",
            f"'{name}' has no clear Google Business Profile in '{city}' — the closest Google match was "
            f"'{_name_of(place)}', a different business (name match {match:.0%}). Finding: you're "
            f"effectively invisible on Google Maps and Search, a critical local-visibility gap. "
            f"Claiming/creating a Google Business Profile is the highest-impact next step.",
        )
        return audit

    place_id = place.get("id")
    audit["business"].update({
        "resolved_name": _name_of(place),
        "place_id": place_id,
        "primary_category": _category_of(place),
        "categories": place.get("types", []),
        "status": place.get("businessStatus"),
    })

    # 2. Full details.
    try:
        details = _place_details(place_id, key, DETAILS_MASK)
        common.add_cost(audit, "places_details", COST_PLACE_DETAILS, "business profile")
    except Exception as e:  # noqa: BLE001
        common.record_error(audit, "fetch_gbp", f"Places details error: {e}")
        details = place  # fall back to the lighter search result

    audit["business"].update({
        "resolved_name": _name_of(details) or audit["business"]["resolved_name"],
        "address": details.get("formattedAddress"),
        "phone": details.get("nationalPhoneNumber"),
        "website": details.get("websiteUri") or audit["business"].get("website"),
        "primary_category": _category_of(details) or audit["business"]["primary_category"],
        "categories": details.get("types", audit["business"]["categories"]),
        "status": details.get("businessStatus", audit["business"]["status"]),
    })
    if details.get("websiteUri") and not audit["website"]["url"]:
        audit["website"]["url"] = details["websiteUri"]

    # Derive a usable search category — Google's primary category is sometimes uselessly generic
    # ("Services"), which would turn competitor + AIO queries into noise ("best services in <city>").
    search_cat, inferred = common.best_category(audit["business"].get("primary_category"), name)
    audit["business"]["search_category"] = search_cat
    if inferred:
        common.record_error(
            audit, "fetch_gbp",
            f"Google lists a generic category ('{audit['business'].get('primary_category')}'); "
            f"inferred '{search_cat}' from the business name for competitor + AIO queries.")

    completeness, missing = _completeness(details)
    current_hours = details.get("currentOpeningHours") or {}
    audit["google_presence"].update({
        "rating": details.get("rating"),
        "review_count": details.get("userRatingCount"),
        "photo_count": len(details.get("photos", []) or []),
        "has_hours": bool(details.get("regularOpeningHours")),
        "open_now": current_hours.get("openNow"),
        "categories_count": len(details.get("types", []) or []),
        "completeness_score": completeness,
        "missing": missing,
    })

    audit["reviews"].update({
        "rating": details.get("rating"),
        "count": details.get("userRatingCount"),
        "recent_sample": _map_reviews(details),
        # Owner responses to reviews are NOT exposed by the Places API — left unknown.
        "responds_to_reviews": None,
    })

    # 3. Competitors — only when we have a SPECIFIC category, else the search returns noise
    # (e.g. "best services in <city>" surfaces unrelated agencies, not real competitors).
    category = audit["business"].get("search_category")
    if common.is_generic_category(category):
        common.record_error(
            audit, "fetch_gbp",
            "No specific business category resolved — skipped competitor search to avoid noise. "
            "Pass --category to enable it.")
        audit["competitors"] = []
        return audit

    try:
        comp = _text_search(f"{category} in {city}", key, COMPETITOR_MASK, max_results=10)
        common.add_cost(audit, "places_text_search", COST_TEXT_SEARCH, "competitors")
    except Exception as e:  # noqa: BLE001
        common.record_error(audit, "fetch_gbp", f"Competitor search error: {e}")
        comp = []

    competitors = []
    for c in comp:
        if c.get("id") == place_id:
            continue  # skip the subject itself
        competitors.append({
            "name": _name_of(c),
            "rating": c.get("rating"),
            "review_count": c.get("userRatingCount"),
            "place_id": c.get("id"),
        })
        if len(competitors) >= 8:
            break
    audit["competitors"] = competitors
    return audit


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch Google profile + competitors for a business.")
    ap.add_argument("--name", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--website", default=None)
    args = ap.parse_args()

    audit = common.new_audit(args.name, args.city, args.website)
    enrich(audit)

    gp = audit["google_presence"]
    print(f"Resolved: {audit['business']['resolved_name']}  ({audit['business']['primary_category']})")
    print(f"Rating: {gp['rating']}  Reviews: {gp['review_count']}  "
          f"Photos: {gp['photo_count']}  Completeness: {gp['completeness_score']}/100")
    print(f"Missing: {', '.join(gp['missing']) or 'nothing'}")
    print(f"Competitors found: {len(audit['competitors'])}")
    for c in audit["competitors"][:5]:
        print(f"  - {c['name']}: {c['rating']}★ ({c['review_count']} reviews)")
    if audit["errors"]:
        print("Errors:", audit["errors"])
    print(f"Est. cost: ${audit['meta']['cost']['total_usd']}")
    common.save_json(audit, common.TMP / f"{common.slugify(args.name)}-gbp.json")


if __name__ == "__main__":
    main()
