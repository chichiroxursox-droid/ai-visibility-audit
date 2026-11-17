"""Guardrails for the public funnel: email validation, rate limiting, spend ceiling,
and lead capture.

A public endpoint that fans out to paid APIs (Places + OpenAI web_search + SerpAPI)
is an unbounded-spend and abuse risk — this bounds it. obs 3471 flagged the engine's
lack of a spend circuit-breaker; here we bound spend at the ENQUEUE boundary: estimate
a per-audit ceiling and cap audits per day, so daily spend <= DAILY_AUDIT_CAP * MAX_AUDIT_COST_USD.

ponytail: in-memory + single-process (rate window and daily counter reset on restart),
a JSONL lead log. Swap the counters for Redis and the log for a DB when we run multiple
workers. A true per-CALL breaker inside the engine is the deeper follow-up; the enqueue
gate covers the public-abuse case at a fraction of the code.
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Deque, Optional

from tools import common

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

RATE_LIMIT_PER_HOUR = int(os.environ.get("RATE_LIMIT_PER_HOUR", "3"))   # audits/hour per key
DAILY_AUDIT_CAP = int(os.environ.get("DAILY_AUDIT_CAP", "50"))          # total audits/day (all users)
MAX_AUDIT_COST_USD = float(os.environ.get("MAX_AUDIT_COST_USD", "0.60"))  # worst-case per audit (observed ~$0.48)
LEADS_PATH = common.ROOT / "data" / "leads.jsonl"


def valid_email(email: Optional[str]) -> bool:
    return bool(email and _EMAIL_RE.match(email.strip()))


class RateLimiter:
    """Sliding-window limiter: at most `per_hour` events per key in any 60-minute window."""

    def __init__(self, per_hour: int = RATE_LIMIT_PER_HOUR) -> None:
        self.per_hour = per_hour
        self._events: dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        now = now if now is not None else datetime.now(timezone.utc).timestamp()
        cutoff = now - 3600
        with self._lock:
            q = self._events.setdefault(key, deque())
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.per_hour:
                return False
            q.append(now)
            return True


class SpendGuard:
    """Bounds total daily spend by capping audits/day (each estimated at MAX_AUDIT_COST_USD)."""

    def __init__(self, daily_cap: int = DAILY_AUDIT_CAP) -> None:
        self.daily_cap = daily_cap
        self._day: Optional[date] = None
        self._count = 0
        self._lock = threading.Lock()

    def _roll(self, today: date) -> None:
        if self._day != today:
            self._day, self._count = today, 0

    def allow(self, today: Optional[date] = None) -> bool:
        today = today or datetime.now(timezone.utc).date()
        with self._lock:
            self._roll(today)
            if self._count >= self.daily_cap:
                return False
            self._count += 1
            return True

    def spent_estimate(self) -> float:
        return round(self._count * MAX_AUDIT_COST_USD, 2)


def record_lead(params: dict[str, Any], path: Path = LEADS_PATH) -> None:
    """Append a lead to the JSONL log (email + what they asked about) so no lead is lost."""
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "at": datetime.now(timezone.utc).isoformat(),
        "email": params.get("email"),
        "track": params.get("track"),
        "name": params.get("name"),
        "city": params.get("city"),
        "category": params.get("category"),
        "website": params.get("website"),
    }
    line = json.dumps(row, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    # Also to stdout: on ephemeral-FS hosts (Render) the file is lost on redeploy, but
    # logs persist — so no lead is silently dropped. ponytail: move to a DB/email at scale.
    print(f"[lead] {line}", flush=True)
