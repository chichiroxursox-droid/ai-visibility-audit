"""Runnable check for the public-funnel guardrails — pure logic, no network.

    python -m api.test_guardrails
"""
from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

from api import guardrails as g


def demo() -> None:
    # email validation
    assert g.valid_email("owner@example.com")
    assert g.valid_email("a.b-c@sub.domain.co")
    assert not g.valid_email("nope")
    assert not g.valid_email("a@b")
    assert not g.valid_email("")
    assert not g.valid_email(None)

    # rate limiter: 3/hour per key, sliding window (drive time with a fixed `now`)
    rl = g.RateLimiter(per_hour=3)
    t = 1_000_000.0
    assert all(rl.allow("k", now=t + i) for i in range(3)), "first 3 should pass"
    assert not rl.allow("k", now=t + 3), "4th within the hour should trip"
    assert rl.allow("k", now=t + 3601), "one falls out of the window after an hour"
    assert rl.allow("other", now=t + 3), "a different key is independent"

    # spend guard: caps audits/day; rolls over on date change
    sg = g.SpendGuard(daily_cap=2)
    d1, d2 = date(2026, 7, 28), date(2026, 7, 29)
    assert sg.allow(today=d1) and sg.allow(today=d1)
    assert not sg.allow(today=d1), "3rd audit same day should trip the cap"
    assert sg.allow(today=d2), "new day resets the counter"

    # lead capture writes a JSONL row
    tmp = Path(tempfile.mkdtemp()) / "leads.jsonl"
    g.record_lead({"email": "o@x.com", "track": "existing", "name": "Joe's Brakes",
                   "city": "Gilbert", "category": "brake repair", "website": "joesbrakes.com"}, path=tmp)
    row = json.loads(tmp.read_text().strip())
    assert row["email"] == "o@x.com" and row["name"] == "Joe's Brakes" and "at" in row, row

    print("guardrails OK: email validation, rate limit, daily spend cap, lead capture")


if __name__ == "__main__":
    demo()
