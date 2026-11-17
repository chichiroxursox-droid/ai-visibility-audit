"""AIO-Citation Kit · orchestrator. One audit.json -> a done-for-you "get cited by AI" kit.

Assembles the three deliverables (Directory-Gap List, FAQ content, JSON-LD Schema), writes the
machine-readable artifacts, and renders a branded HTML delivery doc the operator hands off.
JSON-first like the rest of the engine.

The FAQ is gated: the first run drafts it (status pending_review) and the schema ships WITHOUT the
FAQPage. After a human fact-checks and approves it (`--approve --reviewer "<name>"`), re-running the
kit embeds the approved FAQPage into the deployable schema. A re-run reuses the on-disk faq_draft.json
(preserving the approval and any human edits) unless --redraft is passed.

CLI:
  python -m tools.citation_kit --from .tmp/<slug>/audit.json                         # build (draft FAQ)
  python -m tools.citation_kit --approve --reviewer "Jane Q" --dir .tmp/<slug>       # gate + re-embed
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from tools import common, directory_gap, gen_faq, gen_schema

BRAND = os.environ.get("BRAND_NAME", "Local Visibility")
TEMPLATES_DIR = common.ROOT / "templates"

SECTION_META = [
    ("claim_free_first", "Claim these first — free, fast, AI-trusted",
     "The highest-ROI listings: free, quick to claim, and several are sources the AI assistants already pull from for your query."),
    ("trade_specific", "Trade-specific directories",
     "Industry directories the AI cites for your trade. Most are free or low-cost."),
    ("consider_paid", "Consider these paid platforms",
     "Lead platforms the AI cites — worth it only if one new customer is worth more than the monthly cost."),
    ("participation", "Participation — not a profile to claim",
     "AIs (especially Perplexity) lean heavily on these. Win them with authentic, disclosed participation — answer real local questions, link your pages. Never use fake accounts or spam."),
    ("not_claimable", "Read-only sources",
     "Sources the AI cites that you can't directly claim. Informational — make sure your underlying data feeds them correctly."),
    ("other_cited_sources", "Competitors & other sources the AI pulled from",
     "Who the AI surfaced instead of you. These are your content, PR, and 'get reviewed here' targets — not directory claims."),
]


def build_kit(audit: dict[str, Any], faq: dict[str, Any] | None = None) -> dict[str, Any]:
    gap = directory_gap.build_gap_list(audit)
    if faq is None:
        faq = gen_faq.build_faq(audit)
    # The FAQPage only enters the deployable schema once a human has approved the FAQ.
    faqpage = gen_faq.faqpage_block(faq.get("items") or []) if faq.get("status") == "approved" else None
    schema = gen_schema.build_schema(audit, faqpage=faqpage)
    biz = audit.get("business", {}) or {}
    return {
        "meta": {
            "generated_at": common.now_iso(),
            "business_name": biz.get("resolved_name") or biz.get("query_name"),
            "city": (audit.get("meta", {}).get("query", {}) or {}).get("city"),
            "trade": gap.get("trade"),
            "engine_version": common.ENGINE_VERSION,
        },
        "directory_gap": gap,
        "faq": faq,
        "schema": schema,
    }


def _build_view(kit: dict[str, Any]) -> dict[str, Any]:
    gap = kit["directory_gap"]
    sections = []
    for key, label, desc in SECTION_META:
        rows = gap["sections"].get(key) or []
        if rows:
            sections.append({"key": key, "label": label, "desc": desc, "rows": rows})
    generated = kit["meta"].get("generated_at")
    try:
        gen_dt = datetime.fromisoformat(generated.replace("Z", "+00:00")) if generated else None
        generated_fmt = gen_dt.strftime("%B %-d, %Y") if gen_dt else ""
    except Exception:
        generated_fmt = generated or ""
    return {
        "brand": BRAND,
        "business_name": kit["meta"].get("business_name"),
        "city": kit["meta"].get("city"),
        "trade": kit["meta"].get("trade"),
        "generated": generated_fmt,
        "own_site_cited_by": gap.get("own_site_cited_by") or [],
        "summary": gap.get("summary") or {},
        "sections": sections,
        "faq": kit.get("faq") or {},
        "schema": kit["schema"],
    }


def render(kit: dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html", "xml", "j2"), default_for_string=True),
    )
    return env.get_template("citation_kit.html.j2").render(v=_build_view(kit))


def run(audit_path: str | Path, outdir: Path | None = None, redraft: bool = False) -> dict[str, Any]:
    audit = common.load_json(audit_path)
    outdir = outdir or Path(audit_path).parent
    outdir.mkdir(parents=True, exist_ok=True)

    # Reuse an existing FAQ draft (preserves a logged approval + any human edits); only redraft on ask.
    faq_path = outdir / "faq_draft.json"
    faq = None
    if faq_path.exists() and not redraft:
        try:
            faq = json.loads(faq_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            faq = None

    kit = build_kit(audit, faq=faq)
    common.save_json(kit, outdir / "citation_kit.json")
    common.save_json(kit["directory_gap"], outdir / "gap_list.json")
    faq_path.write_text(json.dumps(kit["faq"], indent=2, ensure_ascii=False), encoding="utf-8")
    (outdir / "schema.jsonld").write_text(
        json.dumps(kit["schema"]["blocks"], indent=2, ensure_ascii=False), encoding="utf-8")
    (outdir / "citation_kit.html").write_text(render(kit), encoding="utf-8")

    g = kit["directory_gap"]["summary"]
    f = kit["faq"]
    faq_state = "APPROVED ✓" if f.get("status") == "approved" else f"{f.get('status')} (needs human gate)"
    print("=" * 64)
    print(f"  AI-Citation Kit · {kit['meta']['business_name']}")
    print(f"  Trade            : {kit['meta']['trade']}")
    print(f"  Sources AI cited : {g.get('sources_ai_cited')}")
    print(f"  Claimable gaps   : {g.get('claimable_gaps')} ({g.get('cited_but_unclaimed')} already AI-cited)")
    print(f"  FAQ              : {f.get('item_count')} Q&A · {f.get('items_flagged')} flagged · {faq_state}")
    print(f"  Schema subtype   : {kit['schema']['subtype']} (valid: {kit['schema']['compliance']['valid']}"
          f", FAQPage: {kit['schema'].get('includes_faqpage')})")
    print(f"  → {outdir / 'citation_kit.html'}")
    if f.get("status") != "approved":
        print("  ↳ FAQ is a DRAFT. After a human fact-checks it, approve to embed the FAQPage:")
        print(f"    python -m tools.citation_kit --approve --reviewer \"<name>\" --dir {outdir}")
    print("=" * 64)
    return kit


def approve(directory: str | Path, reviewer: str) -> dict[str, Any]:
    """Log a human's FAQ fact-check approval, then rebuild the kit so the FAQPage gets embedded."""
    d = Path(directory)
    faq = json.loads((d / "faq_draft.json").read_text(encoding="utf-8"))
    faq, entry = gen_faq.approve_faq(faq, reviewer)
    (d / "faq_draft.json").write_text(json.dumps(faq, indent=2, ensure_ascii=False), encoding="utf-8")
    log_path = gen_faq.append_audit_log(d, entry)
    print(f"✓ FAQ approved by {entry['reviewer']} at {entry['timestamp']} — logged to {log_path}\n")
    return run(d / "audit.json", outdir=d)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build (or gate-approve) the AI-Citation Kit from an audit.json.")
    ap.add_argument("--from", dest="src", help="path to an audit.json")
    ap.add_argument("--out", default=None, help="output dir (defaults to the audit's dir)")
    ap.add_argument("--redraft", action="store_true", help="regenerate the FAQ draft from scratch")
    ap.add_argument("--approve", action="store_true", help="log the human FAQ approval, then re-embed")
    ap.add_argument("--reviewer", help="name of the human who fact-checked (required with --approve)")
    ap.add_argument("--dir", help="business dir (approve mode; defaults to the audit's dir)")
    args = ap.parse_args()

    if args.approve:
        d = args.dir or (Path(args.src).parent if args.src else None)
        if not d:
            ap.error("--approve needs --dir <business dir> (or --from <audit.json>)")
        if not (args.reviewer or "").strip():
            ap.error("--approve needs --reviewer \"<name>\" (the human gate requires a name)")
        approve(d, args.reviewer)
        return

    if not args.src:
        ap.error("provide --from <audit.json> to build, or --approve --dir <dir> to gate the FAQ")
    run(args.src, Path(args.out) if args.out else None, redraft=args.redraft)


if __name__ == "__main__":
    main()
