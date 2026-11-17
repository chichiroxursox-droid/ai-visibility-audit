"""Shared foundation for the AI Visibility Audit engine.

Everything the tools need in common: config/key loading, the canonical `audit`
data model (JSON-first — the renderer and the future web front-end consume only
this shape), a tiny cost tracker, and JSON I/O helpers.

Run any tool as a module so imports resolve, e.g.:
    python -m tools.run_audit --name "Joe's Plumbing" --city "Austin, TX"
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv  # python-dotenv is installed globally
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass

ENGINE_VERSION = "0.1.0"

# Repo root and the disposable intermediate-files dir (per project conventions).
ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"
TMP.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Config / API keys
# ---------------------------------------------------------------------------

# env var name -> what it unlocks (used for human-readable "missing key" notes)
KEY_ENV = {
    "google_maps": "GOOGLE_MAPS_API_KEY",
    "pagespeed": "PAGESPEED_API_KEY",  # optional; PageSpeed works keyless at a lower rate limit
    "openai": "OPENAI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "serpapi": "SERPAPI_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",  # optional free-tier synthesis backend
}


def get_key(name: str, required: bool = False) -> str | None:
    """Return an API key by logical name (see KEY_ENV). None if unset."""
    env_name = KEY_ENV.get(name, name)
    val = os.environ.get(env_name)
    if not val and required:
        raise RuntimeError(
            f"Missing required API key: set {env_name} in .env "
            f"(see .env.example for setup instructions)."
        )
    return val or None


def has_key(name: str) -> bool:
    return bool(get_key(name))


# ---------------------------------------------------------------------------
# Category resolution — Google's primary category is sometimes uselessly generic
# ("Services", "Establishment"), which can't anchor a "best <X> in <city>" query
# or a competitor search. When that happens we infer the real trade from the name.
# ---------------------------------------------------------------------------

GENERIC_CATEGORIES = {
    "services", "service", "establishment", "point of interest", "business",
    "local business", "store", "shopping", "company", "corporation", "general business",
}

# (substring-in-name -> canonical trade). Ordered specific-first.
_TRADE_KEYWORDS = [
    ("pressure wash", "pressure washing service"), ("power wash", "pressure washing service"),
    ("pet groom", "pet grooming"), ("dog groom", "dog grooming"), ("groom", "pet grooming"),
    ("plumb", "plumber"), ("hvac", "hvac contractor"), ("air condition", "hvac contractor"),
    ("heating", "hvac contractor"), ("roofing", "roofing contractor"), ("roofer", "roofing contractor"),
    ("electric", "electrician"), ("landscap", "landscaper"), ("lawn", "lawn care service"),
    ("handyman", "handyman"), ("handy", "handyman"),
    ("house clean", "house cleaning service"), ("maid", "house cleaning service"),
    ("janitor", "cleaning service"), ("clean", "cleaning service"),
    ("auto repair", "auto repair shop"), ("brake", "brake repair"), ("mechanic", "auto repair shop"),
    ("transmission", "auto repair shop"), ("detail", "auto detailing"), ("car wash", "car wash"),
    ("tow", "towing service"), ("locksmith", "locksmith"), ("paint", "painter"),
    ("pest", "pest control"), ("junk removal", "junk removal service"), ("tree", "tree service"),
    ("fenc", "fencing contractor"), ("floor", "flooring contractor"), ("concrete", "concrete contractor"),
    ("garage door", "garage door service"), ("salon", "hair salon"), ("barber", "barber shop"),
    ("massage", "massage therapist"), ("nail", "nail salon"), ("spa", "day spa"),
    ("dental", "dentist"), ("dentist", "dentist"), ("chiropract", "chiropractor"),
    ("notary", "notary service"), ("photograph", "photographer"), ("bakery", "bakery"),
    ("cater", "caterer"), ("moving", "moving company"), ("mover", "moving company"),
    ("window clean", "window cleaning service"), ("gutter", "gutter cleaning service"),
    ("pool", "pool service"), ("appliance repair", "appliance repair service"),
    ("pet sit", "pet sitting service"), ("dog walk", "dog walking service"),
    # coaching / lessons / fitness (single-word signals; "<activity> lessons/coaching" handled below)
    ("personal train", "personal trainer"), ("crossfit", "fitness coaching"),
    ("yoga", "yoga instructor"), ("pilates", "pilates instructor"),
    ("martial art", "martial arts instructor"), ("karate", "martial arts instructor"),
    ("taekwondo", "martial arts instructor"), ("gymnastics", "gymnastics coaching"),
]

# Instruction/coaching businesses rarely have a one-word trade keyword — the trade lives in the NAME
# as "<activity> <service-word>" (e.g. "Main Line Basketball Lessons" -> "basketball lessons").
# Maps the service word to its canonical form; some are usable as a trade on their own.
_INSTRUCTIONAL_SUFFIX = {
    "lessons": "lessons", "lesson": "lessons", "classes": "classes", "class": "classes",
    "coaching": "coaching", "coach": "coaching", "training": "training", "trainer": "training",
    "tutoring": "tutoring", "tutor": "tutoring", "instruction": "lessons", "instructor": "lessons",
    "academy": "coaching",
}
_STANDALONE_INSTRUCTIONAL = {"coaching", "tutoring", "training"}  # fine as a trade without an activity
# brand/location filler that should never be taken as the activity word
_NAME_FILLER = {"the", "and", "of", "for", "your", "main", "line", "elite", "pro", "premier", "greater",
                "metro", "area", "city", "local", "best", "top", "center", "centre", "studio", "club",
                "academy", "company", "group", "team"}


def _instructional_trade(low_name: str) -> str | None:
    """Extract a trade from an instruction/coaching business NAME, e.g. 'main line basketball lessons'
    -> 'basketball lessons', 'smith math tutoring' -> 'math tutoring', 'abc tutoring' -> 'tutoring'."""
    toks = re.findall(r"[a-z]+", low_name)
    for i, t in enumerate(toks):
        if t in _INSTRUCTIONAL_SUFFIX:
            canon = _INSTRUCTIONAL_SUFFIX[t]
            activity = toks[i - 1] if i > 0 else None
            if activity and len(activity) >= 3 and activity not in _NAME_FILLER \
                    and activity not in _INSTRUCTIONAL_SUFFIX:
                return f"{activity} {canon}"
            return canon if canon in _STANDALONE_INSTRUCTIONAL else None
    return None


# Keywords distinctive enough that, in a business NAME, they reliably denote a narrower SPECIALTY
# than a broad Google category (e.g. "Joe's Mobile Brake Repair" is "brake repair", not Google's
# umbrella "Car repair and maintenance service"). Deliberately conservative: only unambiguous,
# rarely-metaphorical terms — so we never hijack a correct specific category (e.g. "Clean Sweep
# Chimney" must stay "Chimney sweep", which is why weak words like "clean"/"paint"/"tree" are excluded).
_SPECIALTY_KEYWORDS = {
    "brake", "transmission", "auto body", "car wash",          # under the auto-repair umbrella
    "pressure wash", "power wash", "garage door", "gutter", "window clean",
    "junk removal", "appliance repair", "pet groom", "dog groom",
    "locksmith", "roofing", "roofer",
}


def best_category(primary: str | None, name: str | None) -> tuple[str | None, bool]:
    """Pick a usable category for 'best <X> in <city>' queries + competitor search.

    Returns (category, inferred). Order of preference:
      1. Google's primary category, if specific — UNLESS the name reveals a narrower specialty the
         category misses (a strong `_SPECIALTY_KEYWORDS` hit, e.g. a brake shop filed as "Car repair").
         Auditing a brake specialist as generic auto repair unfairly buries it among general shops.
      2. If the primary is missing or generic ('Services'), infer the trade from the name.
    `inferred` is True when the result came from the name rather than Google (so callers can flag it).
    """
    p = (primary or "").strip()
    low = (name or "").lower()
    name_trade = name_kw = None
    for kw, trade in _TRADE_KEYWORDS:        # specific-first; take the first match
        if kw in low:
            name_trade, name_kw = trade, kw
            break

    if p and p.lower() not in GENERIC_CATEGORIES:
        # Specific Google category — only override for a strong, additive name specialty.
        if (name_trade and name_kw in _SPECIALTY_KEYWORDS
                and name_kw not in p.lower() and name_trade.lower() != p.lower()):
            return name_trade, True
        return p, False
    # Missing/generic category — infer from the name (trade keyword, then the instructional pattern).
    if name_trade:
        return name_trade, True
    inst = _instructional_trade(low)
    if inst:
        return inst, True
    return (p or None), False


def is_generic_category(category: str | None) -> bool:
    return (not category) or category.strip().lower() in GENERIC_CATEGORIES


# ---------------------------------------------------------------------------
# Cost tracking — every tool records its API spend so run_audit can report it
# ---------------------------------------------------------------------------

class CostTracker:
    """Accumulates estimated USD spend across API calls within one audit."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, service: str, est_usd: float, detail: str = "") -> None:
        self.items.append({"service": service, "est_usd": round(est_usd, 5), "detail": detail})

    @property
    def total(self) -> float:
        return round(sum(i["est_usd"] for i in self.items), 4)

    def as_dict(self) -> dict[str, Any]:
        return {"total_usd": self.total, "items": self.items}


# ---------------------------------------------------------------------------
# The canonical audit data model
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_audit(name: str, city: str, website: str | None = None) -> dict[str, Any]:
    """Scaffold an empty audit dict. Tools fill their own sections in place.

    Sections (all optional until a tool populates them):
      meta              run metadata + cost
      business          identity resolved from Google
      google_presence   profile completeness signals
      reviews           rating/count + recent sample
      competitors       ranked nearby competitors
      website           PageSpeed + schema + on-page gaps
      aio               per-engine AI-visibility results (the differentiator)
      score             0-100 score + breakdown (filled by score.py)
      narrative         human-readable story (filled by score.py)
      fixes             prioritized fix list (filled by score.py)
      errors            non-fatal issues, so the report can still render
    """
    return {
        "meta": {
            "generated_at": now_iso(),
            "engine_version": ENGINE_VERSION,
            "query": {"name": name, "city": city, "website": website},
            "cost": {"total_usd": 0.0, "items": []},
        },
        "business": {
            "query_name": name,
            "resolved_name": None,
            "place_id": None,
            "address": None,
            "phone": None,
            "website": website,
            "categories": [],
            "primary_category": None,
            "status": None,
        },
        "google_presence": {
            "rating": None,
            "review_count": None,
            "photo_count": None,
            "has_hours": None,
            "open_now": None,
            "categories_count": None,
            "completeness_score": None,
            "missing": [],
        },
        "reviews": {
            "rating": None,
            "count": None,
            "recent_sample": [],
            "responds_to_reviews": None,
        },
        "competitors": [],  # list of {name, rating, review_count, place_id}
        "website": {
            "url": website,
            "reachable": None,
            "pagespeed": None,  # {performance, seo, accessibility, best_practices, mobile_friendly, lcp, inp, cls}
            "schema_types": [],
            "has_localbusiness_schema": None,
            "has_faq_schema": None,
            "title": None,
            "meta_description": None,
            "h1": [],
            "has_sitemap": None,
            "has_robots": None,
            "service_pages_found": [],
            "location_pages_found": [],
            "gaps": [],
        },
        "aio": {
            "queries": [],
            "engines": {},  # engine_key -> {asked, mentioned, raw_answer, cited_domains, error}
            "summary": {"mentioned_in": 0, "engines_checked": 0, "all_cited_domains": []},
        },
        "score": None,        # {overall, breakdown:{...}, rubric_notes}
        "narrative": None,    # {headline, vs_competitors, aio_verdict}
        "fixes": [],          # [{priority, title, why, service}]
        "errors": [],         # [{tool, message}]
    }


def record_error(audit: dict[str, Any], tool: str, message: str) -> None:
    audit.setdefault("errors", []).append({"tool": tool, "message": str(message)})


def add_cost(audit: dict[str, Any], service: str, est_usd: float, detail: str = "") -> None:
    """Append an estimated API cost to the audit's running total (meta.cost)."""
    items = audit["meta"]["cost"]["items"]
    items.append({"service": service, "est_usd": round(est_usd, 5), "detail": detail})
    audit["meta"]["cost"]["total_usd"] = round(sum(i["est_usd"] for i in items), 4)


# ---------------------------------------------------------------------------
# JSON I/O
# ---------------------------------------------------------------------------

def save_json(data: Any, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def slugify(text: str) -> str:
    keep = "".join(c.lower() if c.isalnum() else "-" for c in text)
    while "--" in keep:
        keep = keep.replace("--", "-")
    return keep.strip("-") or "audit"
