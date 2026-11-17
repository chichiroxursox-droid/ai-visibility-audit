# Deploying Beacon

Two pieces: the **Python API** (Render, container — it runs headless Chromium) and the
**Next.js frontend** (`web/`, Vercel). They talk over HTTPS; CORS + one env var wire them.

```
Vercel (web/)  ──fetch/SSE──►  Render (Dockerfile: FastAPI + Chromium)
  NEXT_PUBLIC_API_BASE            FRONTEND_ORIGINS must include the Vercel URL
```

## 1. API → Render

1. Push this repo to GitHub (the API deploys from the repo root: `Dockerfile` + `render.yaml`).
2. Render → **New → Blueprint** → select the repo. It reads `render.yaml` and creates
   the `beacon-api` web service (Docker, `starter` plan — Chromium can OOM on free/512MB).
3. In the service's **Environment**, set the `sync: false` secrets from your local `.env`:
   `GOOGLE_MAPS_API_KEY`, `PAGESPEED_API_KEY`, `OPENAI_API_KEY`, `PERPLEXITY_API_KEY`,
   `SERPAPI_KEY`, `GROQ_API_KEY`. Leave `FRONTEND_ORIGINS` for step 3.
4. First deploy builds the image (installs deps + `playwright install --with-deps chromium`
   — a few minutes). When live you get `https://beacon-api.onrender.com`. Check
   `…/openapi.json` returns 200.

> Single worker on purpose: jobs live in-process (`api/jobs.py`). Do **not** raise workers
> or run multiple instances until you move the registry to Redis + Arq — `/status` must
> hit the same process that ran the audit.

## 2. Frontend → Vercel

From `web/`:
```bash
cd web
vercel link          # once
vercel env add NEXT_PUBLIC_API_BASE   # value: https://beacon-api.onrender.com  (all envs)
vercel --prod
```
Vercel auto-detects Next.js 16. You get e.g. `https://beacon.vercel.app`.

## 3. Close the CORS loop

Back in Render, set **`FRONTEND_ORIGINS`** to your Vercel URL(s), comma-separated
(e.g. `https://beacon.vercel.app,https://beacon-<hash>.vercel.app`) and redeploy the API.
The browser can now call the API.

## Verify live
- Open the Vercel URL → **Run a scan** → complete the chat → the report renders.
- Watch Render logs for a `[lead] {...}` line per scan (leads also append to `data/leads.jsonl`,
  which is **ephemeral** on Render — the log line is the durable copy until you add a DB/email).

## Cost & safety (already wired)
- Per audit ≈ **$0.10–0.48** (mostly 6× OpenAI web-search; synthesis is free Groq).
- `DAILY_AUDIT_CAP` × `MAX_AUDIT_COST_USD` bounds daily spend; `RATE_LIMIT_PER_HOUR`
  throttles per email; email is required. Tune via the Render env vars.

## Known upgrade paths (v1 shortcuts, by design)
- **Jobs:** in-process → Redis + Arq for multi-worker/instance + restart persistence.
- **Leads:** JSONL/stdout → a database or an email/Slack notification.
- **Reports:** rendered on demand; keep them ephemeral (Google Places ToS forbids
  long-term caching of profile data).
