"""Phase 5b — the orchestrator. One command, one branded report.

Runs the pipeline in order — Google profile + competitors → website gaps → AI-assistant visibility →
Claude synthesis → branded HTML — writing both the canonical `audit.json` and `report.html`.

Every stage degrades gracefully (missing key or failure is recorded in `audit.errors` and the run
continues), so you always get a report. The run's estimated API cost is logged.

    python -m tools.run_audit --name "Joe's Plumbing" --city "Austin, TX" --website https://joesplumbing.com
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tools import common, fetch_gbp, analyze_site, check_aio, score, render_report


def run(name: str, city: str, website: str | None = None, category: str | None = None,
        outdir: Path | None = None) -> dict:
    audit = common.new_audit(name, city, website)

    print(f"[1/5] Google profile + competitors…")
    fetch_gbp.enrich(audit)

    print(f"[2/5] Website gaps…")
    analyze_site.enrich(audit)

    print(f"[3/5] AI-assistant visibility (ChatGPT · Perplexity · Google AI Overview)…")
    check_aio.enrich(audit, category=category)

    print(f"[4/5] Synthesizing score, narrative & fixes…")
    score.enrich(audit)

    print(f"[5/5] Rendering report…")
    outdir = outdir or (common.TMP / common.slugify(f"{name}-{city}"))
    outdir.mkdir(parents=True, exist_ok=True)
    common.save_json(audit, outdir / "audit.json")
    html = render_report.render(audit)
    (outdir / "report.html").write_text(html, encoding="utf-8")

    s = audit.get("score", {}) or {}
    aio = audit.get("aio", {}).get("summary", {})
    print("\n" + "=" * 60)
    print(f"  {audit['business'].get('resolved_name') or name}")
    print(f"  Visibility score : {s.get('overall')}/100")
    print(f"  AI mentions      : {aio.get('mentioned_in', 0)}/{aio.get('engines_checked', 0)} assistants")
    print(f"  Competitors       : {len(audit.get('competitors', []))}")
    print(f"  Fixes             : {len(audit.get('fixes', []))}")
    print(f"  Est. run cost     : ${audit['meta']['cost']['total_usd']}")
    if audit["errors"]:
        print(f"  Notes/skipped     : {len(audit['errors'])} (see audit.json 'errors')")
    print(f"  → {outdir / 'report.html'}")
    print(f"  → {outdir / 'audit.json'}")
    print("=" * 60)
    return audit


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a full AI Visibility Audit for a local business.")
    ap.add_argument("--name", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--website", default=None)
    ap.add_argument("--category", default=None,
                    help="optional category override for the AI queries (else taken from Google)")
    ap.add_argument("--out", default=None, help="output directory (default .tmp/<slug>)")
    args = ap.parse_args()
    run(args.name, args.city, args.website, args.category,
        Path(args.out) if args.out else None)


if __name__ == "__main__":
    main()
