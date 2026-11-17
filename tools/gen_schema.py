"""AIO-Citation Kit · Deliverable 3 — JSON-LD schema generator + fail-loud validator.

Generates ready-to-deploy schema.org JSON-LD from the audit's verified Google data:
  - LocalBusiness with the SPECIFIC subtype (AutoRepair, Plumber, …) — reduces entity ambiguity
  - Organization (name, url, knowsAbout, @id cross-ref)
  - Service (conditional, service trades)
  - AggregateRating (conditional — ONLY if real review count/value)

Hard rules (from the kit spec §3/§6):
  - Never fabricate. Every value comes from an audit field; absent field → omitted, never invented.
  - `sameAs` is intentionally EMPTY — we don't publish inferred directory URLs; claim them first
    (they live in the Directory-Gap List), then add the verified URLs by hand.
  - A validator FAILS LOUDLY: missing required fields = error; aggregateRating/visible-content =
    a cloaking WARNING the operator must clear before publishing.

CLI:  python -m tools.gen_schema --from .tmp/<slug>/audit.json
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any
from urllib.parse import urlparse

from tools import common

# trade keyword -> a REAL schema.org LocalBusiness subtype (verified to exist). Default LocalBusiness.
_SUBTYPE_MAP = [
    ("brake", "AutoRepair"), ("auto repair", "AutoRepair"), ("mechanic", "AutoRepair"),
    ("car repair", "AutoRepair"), ("transmission", "AutoRepair"), ("tire", "AutoRepair"),
    ("auto body", "AutoBodyShop"), ("auto detail", "AutoWash"), ("car wash", "AutoWash"),
    ("plumb", "Plumber"), ("electric", "Electrician"), ("hvac", "HVACBusiness"),
    ("heating", "HVACBusiness"), ("air condition", "HVACBusiness"), ("roof", "RoofingContractor"),
    ("paint", "HousePainter"), ("locksmith", "Locksmith"), ("moving", "MovingCompany"),
    ("mover", "MovingCompany"), ("general contractor", "GeneralContractor"),
    ("barber", "HairSalon"), ("hair", "HairSalon"), ("nail", "NailSalon"), ("day spa", "DaySpa"),
    ("spa", "DaySpa"), ("salon", "BeautySalon"), ("tattoo", "TattooParlor"),
    ("dentist", "Dentist"), ("dental", "Dentist"), ("restaurant", "Restaurant"),
    ("bakery", "Bakery"), ("coffee", "CafeOrCoffeeShop"), ("cafe", "CafeOrCoffeeShop"),
    # home/construction trades with no narrower schema type -> the construction parent type
    ("pressure wash", "HomeAndConstructionBusiness"), ("power wash", "HomeAndConstructionBusiness"),
    ("handyman", "HomeAndConstructionBusiness"), ("landscap", "HomeAndConstructionBusiness"),
    ("fenc", "HomeAndConstructionBusiness"), ("concrete", "HomeAndConstructionBusiness"),
    ("gutter", "HomeAndConstructionBusiness"), ("garage door", "HomeAndConstructionBusiness"),
    # sports coaching / lessons / fitness -> SportsActivityLocation (a real LocalBusiness subtype)
    ("basketball", "SportsActivityLocation"), ("soccer", "SportsActivityLocation"),
    ("baseball", "SportsActivityLocation"), ("football", "SportsActivityLocation"),
    ("tennis", "SportsActivityLocation"), ("volleyball", "SportsActivityLocation"),
    ("golf", "SportsActivityLocation"), ("hockey", "SportsActivityLocation"),
    ("lacrosse", "SportsActivityLocation"), ("softball", "SportsActivityLocation"),
    ("swim", "SportsActivityLocation"), ("gymnastics", "SportsActivityLocation"),
    ("martial art", "SportsActivityLocation"), ("karate", "SportsActivityLocation"),
    ("yoga", "SportsActivityLocation"), ("pilates", "SportsActivityLocation"),
    ("fitness", "SportsActivityLocation"), ("personal train", "SportsActivityLocation"),
    ("crossfit", "SportsActivityLocation"), ("coaching", "SportsActivityLocation"),
    ("athletic", "SportsActivityLocation"), ("sports", "SportsActivityLocation"),
]
# schema.org LocalBusiness subtypes Google/AI treat as "specific enough" (vs bare LocalBusiness)
_GENERIC_SCHEMA_TYPES = {"localbusiness", "organization", "place", "thing", "automotivebusiness",
                         "homeandconstructionbusiness", "store", "professionalservice"}


def _subtype_for(trade: str | None, primary: str | None) -> str:
    text = f"{trade or ''} {primary or ''}".lower()
    for kw, sub in _SUBTYPE_MAP:
        if kw in text:
            return sub
    return "LocalBusiness"


def _derive_trade(audit: dict[str, Any]) -> str | None:
    q = ((audit.get("aio", {}) or {}).get("queries") or [None])[0] or ""
    m = re.match(r"\s*best\s+(.+?)\s+in\s+", q, re.I)
    if m:
        return m.group(1).strip()
    b = audit.get("business", {}) or {}
    return b.get("search_category") or b.get("primary_category")


def _domain_root(website: str | None) -> str | None:
    if not website:
        return None
    p = urlparse(website if "://" in website else "https://" + website)
    return f"{p.scheme}://{p.netloc}" if p.netloc else None


def _parse_address(addr: str | None) -> dict[str, Any] | None:
    """Parse a Google formatted address into a discrete PostalAddress. None if not parseable."""
    if not addr:
        return None
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    country = "US"
    if parts and re.fullmatch(r"(usa|us|united states)", parts[-1], re.I):
        parts = parts[:-1]
    if len(parts) < 2:
        return None
    # last part should hold "ST 12345"
    region = postal = None
    m = re.search(r"\b([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b", parts[-1])
    if m:
        region, postal = m.group(1), m.group(2)
        locality = parts[-2] if len(parts) >= 2 else None
        street = ", ".join(parts[:-2]) if len(parts) >= 3 else None
    else:
        locality = parts[-1]
        street = ", ".join(parts[:-1]) or None
    pa: dict[str, Any] = {"@type": "PostalAddress"}
    if street:
        pa["streetAddress"] = street
    if locality:
        pa["addressLocality"] = locality
    if region:
        pa["addressRegion"] = region
    if postal:
        pa["postalCode"] = postal
    pa["addressCountry"] = country
    return pa if (street or locality) else None


def build_schema(audit: dict[str, Any], faqpage: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate the JSON-LD blocks + a compliance report. Deterministic; no fabrication.

    `faqpage`: an APPROVED FAQPage block (from gen_faq.faqpage_block) to append. Passed only after a
    human has fact-checked the FAQ; absent on the first (pre-approval) pass, so the deployable schema
    never carries un-vetted Q&A. Even approved, it ships with a visible-on-page (anti-cloaking) warning.
    """
    b = audit.get("business", {}) or {}
    reviews = audit.get("reviews", {}) or {}
    site = audit.get("website", {}) or {}
    name = b.get("resolved_name") or b.get("query_name")
    website = b.get("website")
    phone = b.get("phone")
    trade = _derive_trade(audit)
    primary = b.get("primary_category")
    subtype = _subtype_for(trade, primary)
    root = _domain_root(website)
    city = (audit.get("meta", {}).get("query", {}) or {}).get("city")

    errors: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []

    # --- LocalBusiness ---
    lb: dict[str, Any] = {"@context": "https://schema.org", "@type": subtype, "name": name}
    if website:
        lb["@id"] = (website.rstrip("/") + "#business")
        lb["url"] = website
    if phone:
        lb["telephone"] = phone
    addr = _parse_address(b.get("address"))
    if addr:
        lb["address"] = addr
    else:
        warnings.append("No street address parsed from Google data — used service area only "
                        "(correct for a mobile/service-area business; verify before publishing).")
    if city:
        lb["areaServed"] = city
    # AggregateRating — conditional, real data only
    rating, count = reviews.get("rating"), reviews.get("count")
    if isinstance(rating, (int, float)) and isinstance(count, int) and count > 0:
        lb["aggregateRating"] = {"@type": "AggregateRating",
                                 "ratingValue": rating, "reviewCount": count}
        warnings.append(f"aggregateRating ({rating}★, {count} reviews) reflects the business's GOOGLE "
                        "rating. Publish it ONLY if those reviews (or equivalent on-page testimonials) "
                        "are visible on the page — schema-only ratings are a cloaking violation.")
    else:
        notes.append("aggregateRating omitted — not enough verified review data (correct: empty beats fake).")

    # --- Organization ---
    org: dict[str, Any] = {"@context": "https://schema.org", "@type": "Organization", "name": name}
    if root:
        org["@id"] = root + "#organization"
        org["url"] = website
    knows = []
    for c in (trade, primary):
        if c and c.strip() and not common.is_generic_category(c) and c.strip().title() not in knows:
            knows.append(c.strip().title())
    if knows:
        org["knowsAbout"] = knows

    # --- Service (conditional, service trades) ---
    service = None
    if trade:
        service = {"@context": "https://schema.org", "@type": "Service",
                   "serviceType": trade, "areaServed": city or None}
        service["provider"] = {"@id": lb["@id"]} if lb.get("@id") else {"@type": subtype, "name": name}
        service = {k: v for k, v in service.items() if v is not None}

    # sameAs: intentionally empty (no verified profiles to publish)
    notes.append("sameAs intentionally empty — do NOT publish unverified directory URLs. After you "
                 "CLAIM the profiles in the Directory-Gap List, add their real URLs to sameAs by hand.")

    # specialization note
    existing = [t.lower() for t in (site.get("schema_types") or [])]
    if subtype.lower() not in _GENERIC_SCHEMA_TYPES and any(t in _GENERIC_SCHEMA_TYPES for t in existing) \
            and subtype.lower() not in existing:
        generic_present = next((t for t in existing if t in _GENERIC_SCHEMA_TYPES), "a generic type")
        notes.append(f"Your site currently marks up as '{generic_present}'. This specializes it to "
                     f"'{subtype}', which AI parsers match to intent more precisely.")
    if subtype == "LocalBusiness":
        notes.append("No specific schema.org subtype fits this trade — used the generic LocalBusiness. "
                     "Still valid and useful.")

    # --- validate (fail loud on missing required) ---
    if not name:
        errors.append("Missing business name — cannot generate valid schema.")
    if not (lb.get("address") or lb.get("areaServed")):
        errors.append("Missing both address and service area — LocalBusiness needs at least one.")
    if not website:
        notes.append("No website on file — schema is generated but has nowhere to be deployed yet.")

    blocks = {"localbusiness": lb, "organization": org}
    if service:
        blocks["service"] = service

    # --- FAQPage (conditional, post human-approval only) ---
    faq_entities = (faqpage or {}).get("mainEntity") or []
    if faq_entities:
        blocks["faqpage"] = faqpage
        warnings.append(f"FAQPage carries {len(faq_entities)} approved Q&A. Deploy it ONLY on a page "
                        "where those exact questions and answers are VISIBLE to visitors — schema-only "
                        "FAQ is cloaking. (Note: FAQ rich results were retired May 7 2026; this is for "
                        "AI/assistant parseability, not a Google rich snippet.)")

    graph = {"@context": "https://schema.org", "@graph": [
        {k: v for k, v in lb.items() if k != "@context"},
        {k: v for k, v in org.items() if k != "@context"},
    ] + ([{k: v for k, v in service.items() if k != "@context"}] if service else [])
        + ([{k: v for k, v in faqpage.items() if k != "@context"}] if faq_entities else [])}

    return {
        "subtype": subtype,
        "includes_faqpage": bool(faq_entities),
        "blocks": blocks,
        "script_tag": '<script type="application/ld+json">\n'
                      + json.dumps(graph, indent=2, ensure_ascii=False) + "\n</script>",
        "compliance": {"valid": not errors, "errors": errors, "warnings": warnings, "notes": notes},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate JSON-LD schema from an audit.json.")
    ap.add_argument("--from", dest="src", required=True)
    args = ap.parse_args()
    out = build_schema(common.load_json(args.src))
    print(out["script_tag"])
    print("\n--- compliance ---")
    print(json.dumps(out["compliance"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
