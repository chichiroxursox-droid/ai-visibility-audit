"""In-process job registry for audit runs.

An audit is slow (paid APIs + headless Chromium), so the HTTP handler enqueues a
job and returns immediately; the work runs on a small thread pool and the client
polls /status.

ponytail: single-process, in-memory jobs (lost on restart), ThreadPoolExecutor
capped low for cost control. Swap for Redis + Arq when we need multi-worker
concurrency or job persistence across restarts/deploys.
"""
from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Optional

Runner = Callable[..., dict]  # (name, city, website, category) -> audit dict


def _default_runner(name: str | None = None, city: str = "", website: str | None = None,
                    category: str | None = None, track: str | None = None) -> dict:
    # Lazy import: importing the API shouldn't require the whole engine (and its
    # heavier deps) to be importable — only an actual run does.
    # Q0 fork: a brand-new business has nothing to self-audit -> scan the market instead.
    if track == "new":
        if not category:
            raise ValueError("new-business landscape requires a category")
        from tools import run_landscape
        return run_landscape.run(category, city, name or None, website)
    from tools import run_audit
    if not name:
        raise ValueError("existing-business audit requires a business name")
    return run_audit.run(name, city, website, category)


@dataclass
class Job:
    id: str
    params: dict
    status: str = "queued"   # queued | running | done | error
    audit: Optional[dict] = None
    error: Optional[str] = None


class JobRegistry:
    def __init__(self, runner: Optional[Runner] = None, max_workers: int = 2) -> None:
        self._runner = runner or _default_runner
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, params: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], params=params)
        with self._lock:
            self._jobs[job.id] = job
        self._pool.submit(self._run, job)
        return job

    def _run(self, job: Job) -> None:
        job.status = "running"
        try:
            p = job.params
            job.audit = self._runner(name=p.get("name"), city=p.get("city"),
                                     website=p.get("website"), category=p.get("category"),
                                     track=p.get("track"))
            job.status = "done"
        except Exception as e:  # noqa: BLE001 — any failure surfaces as a job error, never crashes the pool
            job.error = f"{type(e).__name__}: {e}"
            job.status = "error"
            traceback.print_exc()

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)
