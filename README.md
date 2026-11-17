# AI Visibility Audit

Checks whether ChatGPT, Perplexity, and Google's AI Overview recommend a local business, then turns the answer into a fix plan. One command in, one branded HTML report out, for roughly $0.10 to $0.48 of API calls per audit.

## Why it exists

Customers now ask AI assistants for "the best plumber in Austin" instead of scrolling a results page. A small business owner has no way to see what those assistants say about them, or which websites the assistants trust when they answer. This engine measures both and says what to fix.

## How it works

A five-stage pipeline. Every stage reads and writes one JSON `audit` dict (`tools/common.py`), which is the contract the HTML report, the citation kit, and the web API all consume.

```
python -m tools.run_audit --name "Joe's Plumbing" --city "Austin, TX" --website https://example.com

[1/5] fetch_gbp      Google Places API (New): profile, reviews, ranked competitors
[2/5] analyze_site   PageSpeed, schema.org detection, robots.txt, service and location pages
[3/5] check_aio      ChatGPT (web_search) + Perplexity (Sonar) + Google AI Overview (SerpAPI)
[4/5] score          0-100 score, owner-facing narrative, prioritized fix list
[5/5] render_report  self-contained HTML that prints cleanly to PDF
```

Design decisions worth reading:

- **Sample, don't ask once.** AI answers change between identical calls. `check_aio.py` asks 3 phrasings x 2 repeats per engine, concurrently, and reports an appearance rate plus a stability label (`consistent`, `intermittent`, `absent`). Under 30 samples the result is flagged "trend only." Engines are never merged into one number.
- **"Not served" is not "absent."** For local queries Google often shows the map pack and no AI Overview at all. That case is recorded as `not_served` and left out of the average, so a missing surface never reads as a zero.
- **Cited domains are the useful output.** Each engine's cited sources are kept and ranked by frequency. They are the exact third-party sites the AI already trusts for that query, and they drive the citation kit below.
- **Measure the cause, not just the outcome.** The engine records the business's organic rank (AI answers rarely cite outside the top results) and parses `robots.txt` with `urllib.robotparser` to catch sites that block GPTBot, PerplexityBot, ClaudeBot, and other AI crawlers.
- **Never invent a gap.** Site fetching falls back from a plain GET, to a delayed retry, to headless Chromium (Playwright). If a site still can't be read, it is marked unanalyzable and dropped from the score instead of being reported as "missing schema, missing H1."
- **Don't audit a stranger.** Places Text Search returns its nearest match even when the business has no profile. A name-similarity gate (`fetch_gbp._name_match_score`) rejects weak matches. It was added after a live run built a full report around an unrelated company.
- **Degrade, don't crash.** A missing key or failed call is appended to `audit["errors"]` and the run continues. With no keys at all you still get a report.
- **Swappable synthesis.** `AUDIT_SYNTH_BACKEND` picks `anthropic` (default: Claude with structured output), `groq` (free tier), `claude_cli`, or `heuristic`. The heuristic is a weighted average (Google 0.30, reviews 0.25, website 0.20, AI 0.25) renormalized over only the dimensions that could be measured. It is also the fallback when an LLM call fails.
- **Cost ledger.** Every paid call is logged to `meta.cost`, and only successful samples are charged. `AIO_REPEATS` makes the accuracy versus cost tradeoff a config change.

### AIO-Citation Kit

`python -m tools.citation_kit --from .tmp/<slug>/audit.json` turns one audit into three deliverables:

- **Directory-gap list** (`directory_gap.py`, deterministic, no LLM). Joins the cited domains against a curated map of 29 directories (`assets/directories.json`: claim URL, verification method, effort, cost, requirements). Ranks by citation frequency, "the AI cites it but you aren't on it," trade relevance, and a bonus for platforms that feed ChatGPT's local results (Bing Places, Yelp, Facebook). Output is split into compliance-aware sections, so a read-only aggregator or a social site is never treated as a "claim this listing" task.
- **FAQ content** (`gen_faq.py`, the only LLM step). 40 to 60 word, conclusion-first answers per trade and city, the shape assistants tend to quote. The prompt forbids invented prices, hours, licenses, or guarantees, and a post-pass flags any answer that contains one. Every draft starts as `pending_review` and nothing is embedded until a person approves it (`--approve --reviewer "<name>"`), which is logged with a timestamp.
- **JSON-LD schema** (`gen_schema.py`). `LocalBusiness` with the specific subtype, `Organization`, and conditional `Service` and `AggregateRating`, built only from verified audit fields. `sameAs` is left empty on purpose. The validator fails loud on missing required fields and warns when markup has no matching visible content (cloaking).

Design spec: `docs/superpowers/specs/2026-06-19-aio-citation-kit-design.md`.

### Beacon API

`api/` wraps the engine in FastAPI for a public web funnel:

| Route | What it does |
|---|---|
| `POST /chat` | One turn of the onboarding interview (`tools/interview.py`, cheap Groq model) |
| `POST /audit` | Enqueue a job. An existing business gets the full audit; a new business gets a market landscape scan (`tools/run_landscape.py`) |
| `GET /status/{id}` | Poll progress |
| `GET /report/{id}` | The rendered HTML report |

A public endpoint that fans out to paid APIs is a spend risk, so `api/guardrails.py` bounds it when a job is accepted: valid email required, a per-email hourly rate limit, and a daily cap that keeps spend under `DAILY_AUDIT_CAP x MAX_AUDIT_COST_USD`. Jobs run in an in-process thread pool, and the server runs one Uvicorn worker on purpose, because `/status` must hit the process that ran the job. The backend is a container (Render) rather than serverless because the site reader needs Chromium. See `DEPLOY.md`.

## Stack

Python 3.12, requests, BeautifulSoup + lxml, extruct, Playwright (Chromium), Jinja2, Pydantic, FastAPI + Uvicorn. APIs: Google Places API (New), PageSpeed Insights, OpenAI Responses API with `web_search`, Perplexity Sonar, SerpAPI, Anthropic, Groq. Docker and a Render Blueprint for deployment.

Built with Claude Code.

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
python -m playwright install chromium   # only needed for the headless-browser fallback
cp .env.example .env                    # add the keys you have; every one is optional
```

Tests (offline, no API calls, no spend):

```bash
python -m pytest tests -q
python -m tools.test_interview
python -m tools.test_landscape
python -m api.test_guardrails
python -m api.test_seam
```

Run an audit and build the kit (this calls the paid APIs for every key you set):

```bash
python -m tools.run_audit --name "Joe's Plumbing" --city "Austin, TX" --website https://example.com
python -m tools.citation_kit --from .tmp/joe-s-plumbing-austin-tx/audit.json
```

Outputs land in `.tmp/<slug>/`. Start the API with `uvicorn api.main:app --reload`.

## Status and known limits

- The engine and the citation kit work and have been run against real businesses. Those reports are not published here.
- Six samples per engine is a trend, not a metric. AI answers vary a lot month to month. A real trend line needs a weekly panel of about 30 prompts, which is specified in the kit spec but not built.
- Schema markup ships for Google rich results, not as an AI-citation lever. A controlled study found no AI-citation lift from adding JSON-LD, so the kit does not claim one.
- The API's job registry, rate limiter, and spend counter live in memory in one process. They reset on restart, and scaling past one worker needs Redis + Arq first.
- The Next.js frontend for Beacon is not in this repo, and Beacon has not been deployed.
- Google Places data may not be cached long-term (Places ToS), so reports are regenerated, not stored.
