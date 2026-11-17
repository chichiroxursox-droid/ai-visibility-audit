"""Runnable seam check for the audit API — NO paid APIs.

Injects a fake runner so we verify the wiring (POST -> job -> status -> report)
and the real Jinja render path, without spending on Google/OpenAI/SerpAPI/Anthropic.

    python -m api.test_seam
"""
from __future__ import annotations

import time

from tools import common
import api.main as m
import api.guardrails as g
from api.jobs import JobRegistry


def _fake_runner(name=None, city="", website=None, category=None, track=None):
    """Return a minimal-but-valid audit dict (the common.new_audit contract)."""
    audit = common.new_audit(name, city, website)
    audit["business"]["resolved_name"] = name
    audit["score"] = {"overall": 42,
                      "breakdown": {"google": 40, "reviews": 50, "website": 30, "aio": 10}}
    return audit


def demo() -> None:
    from fastapi.testclient import TestClient  # needs httpx; imported here so the message is clear

    m.registry = JobRegistry(runner=_fake_runner)  # module-global swap; endpoints read it at call time
    m.rate_limiter = g.RateLimiter(per_hour=1000)  # keep the seam test hermetic vs guardrails
    m.spend_guard = g.SpendGuard(daily_cap=1_000_000)
    g.record_lead = lambda *a, **k: None           # don't write lead files from the seam test
    client = TestClient(m.app)

    r = client.post("/audit", json={"name": "Joe's Plumbing", "city": "Austin, TX",
                                    "email": "owner@example.com"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    s = {}
    for _ in range(200):  # fake runner is instant; poll briefly for the background thread
        s = client.get(f"/status/{job_id}").json()
        if s["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert s.get("status") == "done", s
    assert s.get("score") == 42, s

    rep = client.get(f"/report/{job_id}")
    assert rep.status_code == 200, rep.text
    assert "<html" in rep.text.lower(), "renderer did not return an HTML document"
    assert "Joe" in rep.text, "business name missing from report"

    assert client.get("/status/does-not-exist").status_code == 404
    assert client.get("/report/does-not-exist").status_code == 404

    print("api seam OK: POST /audit -> /status(done, score=42) -> /report renders HTML; 404s handled")


if __name__ == "__main__":
    demo()
