"""FastAPI seam over the audit engine (milestone 1).

The JSON `audit` dict (tools.common.new_audit) is the contract; this layer just
enqueues runs and serves the existing Jinja report against the result.

Run from the project root so `from tools import ...` resolves:
    uvicorn api.main:app --reload
"""
# No `from __future__ import annotations` here — keeps Pydantic model field
# resolution simple across Pydantic v1/v2.

import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from api import guardrails
from api.jobs import JobRegistry

app = FastAPI(title="Get Found — audit API")

# The Next.js frontend calls this API from the browser — allow its origin(s).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in
                   os.environ.get("FRONTEND_ORIGINS", "http://localhost:3000").split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

registry = JobRegistry()
rate_limiter = guardrails.RateLimiter()
spend_guard = guardrails.SpendGuard()


class ChatTurn(BaseModel):
    role: str      # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatTurn] = []   # transcript so far, WITHOUT the system prompt


@app.post("/chat")
def chat(req: ChatRequest):
    # Cheap-model interview turn: returns the next reply + the structured interview
    # extracted so far + whether we have enough to run the audit/landscape.
    from tools import interview
    history = [t.model_dump() for t in req.messages]
    return interview.step(history)


class AuditRequest(BaseModel):
    city: str
    email: str                       # captured lead; format-validated in the guardrails milestone
    track: Optional[str] = None      # "new" -> market landscape; else -> full audit (Q0 fork)
    name: Optional[str] = None       # required for existing; optional working name for new
    website: Optional[str] = None
    category: Optional[str] = None    # required for new; else optional "best <X> in <city>" override


@app.post("/audit")
def create_audit(req: AuditRequest):
    p = req.model_dump()
    if not guardrails.valid_email(p.get("email")):
        raise HTTPException(status_code=400, detail="a valid email is required")
    # Q0-specific required fields -> clean 400s (defense in depth; the runner also checks).
    if p.get("track") == "new":
        if not (p.get("category") or "").strip():
            raise HTTPException(status_code=400, detail="a category is required for a new-business market scan")
    elif not (p.get("name") or "").strip():
        raise HTTPException(status_code=400, detail="a business name is required")
    if not rate_limiter.allow(p["email"].strip().lower()):
        raise HTTPException(status_code=429, detail="too many audits from this email in the last hour — please wait")
    if not spend_guard.allow():
        raise HTTPException(status_code=429, detail="daily audit capacity reached — please try again tomorrow")
    guardrails.record_lead(p)   # capture the lead before the run so none is lost
    job = registry.submit(p)
    return {"job_id": job.id, "status": job.status}


@app.get("/status/{job_id}")
def status(job_id: str):
    job = registry.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job")
    out = {"job_id": job.id, "status": job.status}
    if job.status == "done" and job.audit:
        out["score"] = (job.audit.get("score") or {}).get("overall")
    elif job.status == "error":
        out["error"] = job.error
    return out


@app.get("/report/{job_id}", response_class=HTMLResponse)
def report(job_id: str):
    job = registry.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job")
    if job.status != "done" or not job.audit:
        raise HTTPException(status_code=409, detail=f"report not ready (status={job.status})")
    # Reuse the existing renderer — audit vs. new-business market scan (Q0 fork).
    from tools import render_report
    if (job.audit.get("meta") or {}).get("mode") == "landscape":
        return render_report.render_landscape(job.audit)
    return render_report.render(job.audit)
