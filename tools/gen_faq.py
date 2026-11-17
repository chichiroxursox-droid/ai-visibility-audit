"""AIO-Citation Kit · Deliverable 2 — FAQ content generator (LLM draft → MANDATORY human gate).

Drafts trade- and city-tailored Q&A that AI assistants can quote verbatim when someone asks about
this business's trade. Every answer is conclusion-first and ~40–60 words — the shape the assistants
actually extract (≈44% of LLM citations come from the first 30% of a passage, so we front-load).

This is the ONE deliverable in the kit that uses a model, and it is gated:
  * Output is always status="pending_review". Nothing here is published or embedded in schema until
    a human fact-checks it and logs an approval (name + timestamp -> audit_log.json).
  * The model is forbidden from inventing business specifics (price, hours, "24/7", "licensed",
    "guaranteed", years in business…). A post-pass FLAGS every answer that contains such a claim so
    the reviewer can confirm or cut it. Unverified scaled FAQ is the #1 spam-signal risk (SpamBrain).
  * FAQPage JSON-LD is only built from APPROVED items, and only deploy-able if the Q&A is visible on
    the page (schema-only = cloaking). Rich results for FAQ were removed May 7 2026 — we build this
    for parseability by Perplexity/Copilot/AI-Overviews, NOT as a promised ranking lever.

Pipeline:
  build_faq(audit)            -> draft dict (status pending_review), Groq-drafted + post-validated
  approve_faq(faq, reviewer)  -> stamps approved, returns the audit_log entry (name + ts)
  faqpage_block(items)        -> FAQPage JSON-LD (call ONLY with approved items)

CLI:
  python -m tools.gen_faq --from .tmp/<slug>/audit.json                       # draft -> faq_draft.json
  python -m tools.gen_faq --approve --reviewer "Jane Q" --dir .tmp/<slug>     # log the human approval
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from tools import common

TEMPLATES_PATH = common.ROOT / "assets" / "faq-templates.json"
GROQ_MODEL = os.environ.get("AUDIT_GROQ_MODEL", "llama-3.3-70b-versatile")
TARGET_COUNT = int(os.environ.get("FAQ_COUNT", "12"))
WORD_MIN, WORD_MAX = 40, 60

# trade keyword -> template family in assets/faq-templates.json
_FAMILY_MAP = [
    ("brake", "auto_repair"), ("auto repair", "auto_repair"), ("mechanic", "auto_repair"),
    ("car repair", "auto_repair"), ("transmission", "auto_repair"), ("tire", "auto_repair"),
    ("auto", "auto_repair"), ("pressure wash", "pressure_washing"), ("power wash", "pressure_washing"),
    ("soft wash", "pressure_washing"), ("window clean", "cleaning"), ("house clean", "cleaning"),
    ("maid", "cleaning"), ("janitor", "cleaning"), ("clean", "cleaning"),
    ("plumb", "plumbing"), ("drain", "plumbing"), ("water heater", "plumbing"),
    ("electric", "electrical"), ("hvac", "hvac"), ("heating", "hvac"), ("air condition", "hvac"),
    ("furnace", "hvac"), ("roof", "roofing"), ("landscap", "landscaping"), ("lawn", "landscaping"),
    ("tree", "landscaping"), ("groom", "pet_services"), ("pet", "pet_services"),
    ("dog", "pet_services"), ("salon", "beauty_salon"), ("barber", "beauty_salon"),
    ("hair", "beauty_salon"), ("nail", "beauty_salon"), ("spa", "beauty_salon"),
    ("mov", "moving"), ("handy", "home_services"), ("contractor", "home_services"),
    ("paint", "home_services"), ("fenc", "home_services"), ("remodel", "home_services"),
    # coaching / lessons / tutoring / fitness instruction
    ("basketball", "coaching"), ("soccer", "coaching"), ("baseball", "coaching"),
    ("football", "coaching"), ("tennis", "coaching"), ("volleyball", "coaching"),
    ("golf", "coaching"), ("hockey", "coaching"), ("lacrosse", "coaching"),
    ("softball", "coaching"), ("gymnastics", "coaching"), ("martial", "coaching"),
    ("karate", "coaching"), ("yoga", "coaching"), ("pilates", "coaching"),
    ("fitness", "coaching"), ("personal train", "coaching"), ("swim", "coaching"),
    ("dance", "coaching"), ("music", "coaching"), ("guitar", "coaching"),
    ("piano", "coaching"), ("voice", "coaching"), ("art", "coaching"),
    ("coaching", "coaching"), ("coach", "coaching"), ("lesson", "coaching"),
    ("tutor", "coaching"), ("training", "coaching"), ("trainer", "coaching"),
    ("instruct", "coaching"), ("academy", "coaching"), ("athletic", "coaching"),
    ("sports", "coaching"),
]

# Claims a small business CANNOT make in an AI-citable answer unless the audit proves them.
# A match doesn't reject the answer — it FLAGS it so the human reviewer confirms or cuts the claim.
_RISK_PATTERNS = [
    ("price", re.compile(
        r"\$\s?\d|\b\d+\s?(?:dollars|usd|bucks)\b|\baffordable\b|\bcheap(?:est|er)?\b|"
        r"\bfree\s+(?:estimate|quote|inspection|consultation|assessment|service)\b|\blowest\s+price", re.I)),
    ("availability", re.compile(
        r"\b24/?7\b|\b24 hours\b|\bsame[- ]day\b|\bemergenc(?:y|ies)\b|\banytime\b|"
        r"\baround the clock\b|\bnext[- ]day\b", re.I)),
    ("response_time", re.compile(r"\bwithin\s+\d|\b\d+\s?(?:minute|min|hour|hr)s?\b", re.I)),
    ("credential", re.compile(
        r"\blicens|\binsur|\bbonded\b|\bcertif|\bA\.?S\.?E\.?\b|\bEPA\b|\baccredit|\bwarrant|"
        r"\bguarante|\bBBB\b", re.I)),
    ("experience", re.compile(
        r"\b\d+\+?\s?years?\b|\bfamily[- ]owned\b|\bsince\s+\d{4}\b|\baward|\bvoted\b|\b#\s?1\b|"
        r"\bdecades?\b", re.I)),
]

SYSTEM = (
    "You write the FAQ section for a small local business's website. The goal: when a real customer "
    "asks ChatGPT, Perplexity, or Google's AI about this trade, these answers are substantive and clean "
    "enough for the AI to quote directly. Write like the owner talking to a neighbor — warm, plain, "
    "genuinely helpful. No marketing fluff, no keyword stuffing, no robotic phrasing.\n"
    "BE GENUINELY USEFUL using GENERAL TRADE KNOWLEDGE. This is what makes an answer worth quoting: "
    "explain the how and the why — what the warning signs are, how the process works, what affects "
    "timing or outcome, how to prepare. General, accurate industry knowledge is ENCOURAGED and is NOT "
    "a fabrication (e.g. for brakes: squealing, grinding, a soft pedal, or vibration when stopping are "
    "common signs service is due). A vague answer like 'contact us for details' is a FAILURE — only "
    "fall back to 'contact us' for the one specific the customer must ask about (their exact price or "
    "time).\n"
    "ANSWER FORMAT (every single answer, no exceptions): write TWO TO THREE complete sentences totaling "
    "45 to 60 words. A one-sentence answer, or anything under 45 words, is too thin — add a second and "
    "third sentence of useful detail (a 'why', a 'what to expect', or a practical tip) until you reach "
    "the range. CONCLUSION FIRST: the direct answer in the opening sentence, then the supporting detail. "
    "Fully self-contained: it must make sense quoted on its own, without the question next to it.\n"
    "The DISTINCTION that matters: general trade knowledge = good. THIS BUSINESS's specific claims that "
    "aren't verified = forbidden. NEVER invent, for THIS business, any of: prices, costs, dollar "
    "amounts, 'free estimate', 'affordable'; hours, '24/7', 'same-day', 'emergency', or response times; "
    "credentials like licensed/insured/bonded/certified/warranty/guaranteed/BBB; years in business, "
    "'family-owned', 'since YYYY', awards, or staff/fleet size — unless they appear in 'Verified facts'. "
    "If an answer would need one of those, explain the general principle and say the customer can "
    "confirm specifics when they book — never state the specific yourself."
)

_EXAMPLE = (
    "Example of the right depth and length (≈50 words, general knowledge, no invented specifics):\n"
    "Q: How do I know if I need new brakes?\n"
    "A: Watch for a few clear signs: a high-pitched squeal when you brake, a grinding sound, a brake "
    "pedal that feels soft or sinks, or vibration when you slow down. Any of these means it's time to "
    "have your brakes inspected — catching it early keeps a simple pad replacement from becoming costly "
    "rotor damage."
)


def _load_templates() -> dict[str, Any]:
    return json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))


def _template_family(trade: str | None, primary: str | None) -> str:
    text = f"{trade or ''} {primary or ''}".lower()
    for kw, fam in _FAMILY_MAP:
        if kw in text:
            return fam
    return "home_services"


def _derive_trade(audit: dict[str, Any]) -> str | None:
    q = ((audit.get("aio", {}) or {}).get("queries") or [None])[0] or ""
    m = re.match(r"\s*best\s+(.+?)\s+in\s+", q, re.I)
    if m:
        return m.group(1).strip()
    b = audit.get("business", {}) or {}
    cat = b.get("search_category") or b.get("primary_category")
    return None if common.is_generic_category(cat) else cat


def _grounded_facts(audit: dict[str, Any], trade: str | None, city: str | None) -> dict[str, Any]:
    """The ONLY business-specific facts the model is allowed to assert."""
    b = audit.get("business", {}) or {}
    reviews = audit.get("reviews", {}) or {}
    name = b.get("resolved_name") or b.get("query_name")
    facts: dict[str, Any] = {"business_name": name}
    if trade:
        facts["trade"] = trade
    if city:
        facts["serves_area"] = city
    if b.get("phone"):
        facts["contact"] = "reachable by phone (customers can call or message to book)"
    rating, count = reviews.get("rating"), reviews.get("count")
    if isinstance(rating, (int, float)) and isinstance(count, int) and count > 0:
        facts["reputation"] = f"rated {rating}★ across {count} Google reviews"
    return facts


def _review_themes(audit: dict[str, Any], n: int = 5) -> list[str]:
    sample = (audit.get("reviews", {}) or {}).get("recent_sample") or []
    out = []
    for r in sample[:n]:
        t = (r.get("text") or "").strip().replace("\n", " ")
        if t:
            out.append(t[:300])
    return out


def _facts_block(facts: dict[str, Any]) -> str:
    return "\n".join(f"- {v}" for v in facts.values()) or "- (no verified specifics — keep every answer general)"


def _seed_questions(family: str, templates: dict[str, Any]) -> list[str]:
    return (templates.get("universal") or []) + (templates.get("by_trade", {}).get(family) or [])


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def _flags_for(answer: str, from_template: bool) -> list[str]:
    flags = [label for label, rx in _RISK_PATTERNS if rx.search(answer or "")]
    wc = _word_count(answer)
    if wc < WORD_MIN - 5 or wc > WORD_MAX + 10:
        flags.append("length")
    if from_template:
        flags.append("template")
    return flags


def _validate_item(question: str, answer: str, from_template: bool = False) -> dict[str, Any]:
    answer = (answer or "").strip()
    return {
        "question": (question or "").strip(),
        "answer": answer,
        "word_count": _word_count(answer),
        "flags": _flags_for(answer, from_template),
    }


def _groq_draft(facts: dict[str, Any], seeds: list[str], themes: list[str],
                key: str, n: int) -> list[dict[str, Any]]:
    from groq import Groq
    client = Groq(api_key=key)
    user = (
        f"Write {n} FAQ question-and-answer pairs for this business.\n\n"
        f"Verified facts (the ONLY business-specific things you may assert):\n{_facts_block(facts)}\n\n"
        "Question ideas (topics real customers ask — adapt the wording, pick the most useful, do not "
        "repeat near-duplicates):\n" + "\n".join(f"- {q}" for q in seeds) + "\n\n"
        + (("What customers actually mention in reviews (use for tone and topic ideas only — do NOT "
            "quote them as facts):\n" + "\n".join(f'- "{t}"' for t in themes) + "\n\n") if themes else "")
        + _EXAMPLE + "\n\n"
        + 'Return ONLY a JSON object of this exact shape: '
          '{"faqs": [{"question": "...", "answer": "..."}, ...]}. '
          f"Provide exactly {n} pairs. Every answer MUST be 2-3 sentences and 45-60 words, conclusion-"
          "first — lean on general trade knowledge so each answer is genuinely informative, never a "
          "vague one-line 'contact us'. Short answers will be rejected."
    )
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        response_format={"type": "json_object"}, temperature=0.5, max_tokens=4000,
    )
    data = json.loads(resp.choices[0].message.content)
    pairs = data.get("faqs") or data.get("questions") or []
    return [_validate_item(p.get("question", ""), p.get("answer", "")) for p in pairs if p.get("question")]


def _template_fallback(facts: dict[str, Any], seeds: list[str], n: int) -> list[dict[str, Any]]:
    """No model available -> emit the seed questions with explicit WRITE-ME placeholders, all flagged
    'template' so the human gate never lets a placeholder reach a published page."""
    name = facts.get("business_name") or "this business"
    items = []
    for q in seeds[:n]:
        placeholder = (f"[DRAFT PLACEHOLDER — the AI drafting backend was unavailable. Write a 40-60 "
                       f"word, conclusion-first answer for {name} to: “{q}”, using only "
                       f"verified facts.]")
        items.append(_validate_item(q, placeholder, from_template=True))
    return items


def build_faq(audit: dict[str, Any], n: int = TARGET_COUNT) -> dict[str, Any]:
    """Draft the FAQ. Always returns status='pending_review' — never publishable as-is."""
    b = audit.get("business", {}) or {}
    name = b.get("resolved_name") or b.get("query_name")
    city = (audit.get("meta", {}).get("query", {}) or {}).get("city")
    trade = _derive_trade(audit)
    templates = _load_templates()
    family = _template_family(trade, b.get("primary_category"))
    facts = _grounded_facts(audit, trade, city)
    seeds = _seed_questions(family, templates)
    themes = _review_themes(audit)

    backend = "template_fallback"
    note = None
    key = common.get_key("groq")
    items: list[dict[str, Any]] = []
    if key:
        try:
            items = _groq_draft(facts, seeds, themes, key, n)
            backend = f"groq:{GROQ_MODEL}"
        except Exception as e:  # noqa: BLE001 - degrade to templates, never crash the kit
            note = f"Groq draft failed ({type(e).__name__}: {e}); emitted template placeholders instead."
    if not items:
        items = _template_fallback(facts, seeds, n)
        if not note and not key:
            note = "GROQ_API_KEY not set — emitted template placeholders for a human to write."

    flagged = sum(1 for it in items if it["flags"])
    return {
        "status": "pending_review",
        "generated_at": common.now_iso(),
        "engine_version": common.ENGINE_VERSION,
        "backend": backend,
        "note": note,
        "business_name": name,
        "trade": trade,
        "city": city,
        "template_family": family,
        "grounded_facts": facts,
        "sourced_from_reviews": bool(themes),
        "items": items,
        "item_count": len(items),
        "items_flagged": flagged,
        "review_required": True,
        "approved_by": None,
        "approved_at": None,
        "gate_notice": ("HUMAN FACT-CHECK REQUIRED. These answers are a draft. Confirm every flagged "
                        "claim against reality, fix anything generic, then approve. Only approved Q&A "
                        "may be published or added to schema — and only if it is visible on the page."),
    }


def approve_faq(faq: dict[str, Any], reviewer: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Log a human's fact-check approval. Mutates + returns faq, plus the audit_log entry to persist."""
    reviewer = (reviewer or "").strip()
    if not reviewer:
        raise ValueError("A reviewer name is required to approve the FAQ (the human gate).")
    if not faq.get("items"):
        raise ValueError("No FAQ items to approve.")
    ts = common.now_iso()
    faq["status"] = "approved"
    faq["approved_by"] = reviewer
    faq["approved_at"] = ts
    entry = {
        "action": "faq_approved",
        "reviewer": reviewer,
        "timestamp": ts,
        "item_count": faq.get("item_count"),
        "items_flagged": faq.get("items_flagged"),
        "backend": faq.get("backend"),
        "engine_version": common.ENGINE_VERSION,
    }
    return faq, entry


def faqpage_block(items: list[dict[str, Any]]) -> dict[str, Any]:
    """FAQPage JSON-LD from APPROVED items. Caller must ensure the Q&A is visible on the page."""
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": it["question"],
             "acceptedAnswer": {"@type": "Answer", "text": it["answer"]}}
            for it in items if it.get("question") and it.get("answer")
        ],
    }


def append_audit_log(directory: str | Path, entry: dict[str, Any]) -> Path:
    """Append one entry to the directory's audit_log.json (the human-gate ledger)."""
    path = Path(directory) / "audit_log.json"
    log = []
    if path.exists():
        try:
            log = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            log = []
    log.append(entry)
    path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _print_review_doc(faq: dict[str, Any]) -> None:
    print("=" * 68)
    print(f"  FAQ DRAFT (status: {faq['status']}) · {faq['business_name']}")
    print(f"  backend: {faq['backend']}   items: {faq['item_count']}   flagged: {faq['items_flagged']}")
    if faq.get("note"):
        print(f"  note: {faq['note']}")
    print("=" * 68)
    print(f"\n  {faq['gate_notice']}\n")
    for i, it in enumerate(faq["items"], 1):
        flag = f"  ⚠ verify: {', '.join(it['flags'])}" if it["flags"] else ""
        print(f"[{i:>2}] ({it['word_count']}w){flag}\n  Q: {it['question']}\n  A: {it['answer']}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Draft (or approve) the kit's FAQ content.")
    ap.add_argument("--from", dest="src", help="path to an audit.json (draft mode)")
    ap.add_argument("--approve", action="store_true", help="approve an existing faq_draft.json")
    ap.add_argument("--reviewer", help="name of the human who fact-checked (required with --approve)")
    ap.add_argument("--dir", help="business dir holding faq_draft.json (approve mode)")
    args = ap.parse_args()

    if args.approve:
        d = Path(args.dir or (Path(args.src).parent if args.src else "."))
        faq = json.loads((d / "faq_draft.json").read_text(encoding="utf-8"))
        faq, entry = approve_faq(faq, args.reviewer or "")
        (d / "faq_draft.json").write_text(json.dumps(faq, indent=2, ensure_ascii=False), encoding="utf-8")
        log_path = append_audit_log(d, entry)
        print(f"✓ FAQ approved by {entry['reviewer']} at {entry['timestamp']} — logged to {log_path}")
        print("  Re-run the kit to embed the approved FAQPage into the schema + delivery doc:")
        print(f"  python -m tools.citation_kit --from {d / 'audit.json'}")
        return

    if not args.src:
        ap.error("provide --from <audit.json> to draft, or --approve --dir <dir> to approve")
    audit = common.load_json(args.src)
    faq = build_faq(audit)
    out = Path(args.src).parent / "faq_draft.json"
    out.write_text(json.dumps(faq, indent=2, ensure_ascii=False), encoding="utf-8")
    _print_review_doc(faq)
    print(f"  → wrote {out}")


if __name__ == "__main__":
    main()
