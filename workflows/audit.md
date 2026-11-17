# Workflow: Run an AI Visibility Audit

**Objective:** Given a local business, produce a branded HTML report (the sales asset) showing how
visible it is on Google and in AI assistants vs. competitors, with a prioritized fix list.

## Inputs
- Business name (as it appears on Google)
- City / location (e.g. "Austin, TX")
- Website URL (optional but strongly recommended)

## Prerequisites
- `.env` populated (see `.env.example`). Minimum to produce *something*: none (PageSpeed works
  keyless). For a full report you want `GOOGLE_MAPS_API_KEY`, the three AIO keys
  (`OPENAI_API_KEY`, `PERPLEXITY_API_KEY`, `SERPAPI_KEY`), and `ANTHROPIC_API_KEY`.

## Tools (run in this order; `run_audit` does it for you)
1. `tools/fetch_gbp.py` — Google profile + competitors  → fills `business`, `google_presence`, `reviews`, `competitors`
2. `tools/analyze_site.py` — website gaps               → fills `website`
3. `tools/check_aio.py` — ChatGPT / Perplexity / Google AI Overview → fills `aio`
4. `tools/score.py` — Claude synthesis                  → fills `score`, `narrative`, `fixes`
5. `tools/render_report.py` — JSON → branded HTML

## One-shot
```
python -m tools.run_audit --name "Joe's Plumbing" --city "Austin, TX" --website https://joesplumbing.com
```
Outputs: `.tmp/<slug>/audit.json` and `.tmp/<slug>/report.html`. Open the HTML; print to PDF to share.

## Outputs
- `audit.json` — the structured data (also what the future web front-end will render)
- `report.html` — the branded, shareable sales report (includes required Google attribution)
- Logged estimated cost for the run

## Edge cases
- **Business not found on Google:** fetch_gbp records an error; that itself is a finding ("you're
  not even showing up") — the report should say so.
- **No website given / unreachable:** analyze_site skips, marks website gaps as "no site found"
  (a major fix recommendation in itself).
- **AI Overview absent for the query:** not all queries trigger one; record `present: false`.
- **Missing API key:** the relevant section is skipped and noted in `errors`; the run still completes.
- **Paid APIs:** confirm before running large batches.
