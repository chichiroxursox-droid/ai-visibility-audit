"""Runnable check for the interview driver — NO model API calls.

Injects a fake chat_fn so the chat + extraction + readiness logic is exercised
deterministically and for free.

    python -m tools.test_interview
"""
from __future__ import annotations

import json

from tools import interview


def _fake_chat(messages, json_mode=False, temperature=0.5, max_tokens=1024):
    if json_mode:  # the extraction call
        return json.dumps({
            "track": "existing",
            "business": {"name": "Joe's Brakes", "city": "Gilbert, AZ",
                         "category": "brake repair", "website": "https://joesbrakes.com"},
            "outcome": "steady full schedule for two vans",
            "levers": {"price": "$300 avg job", "constraint": "demand-bound"},
            "notes": None,
        })
    return "Got it — a brake shop in Gilbert. What would make year three feel like a win?"


def demo() -> None:
    # 1. Empty history -> deterministic greeting, no model call, not ready.
    first = interview.step([])
    assert first["reply"] == interview.GREETING, first["reply"]
    assert first["audit_ready"] is False
    assert first["interview"]["track"] is None

    # 2. A real turn -> fake reply + parsed extraction + ready.
    turn = interview.step(
        [{"role": "user", "content": "I run Joe's Brakes, a brake shop in Gilbert AZ, joesbrakes.com"}],
        chat_fn=_fake_chat,
    )
    assert turn["reply"].startswith("Got it"), turn["reply"]
    iv = turn["interview"]
    assert iv["track"] == "existing", iv
    assert iv["business"]["name"] == "Joe's Brakes", iv
    assert iv["business"]["city"] == "Gilbert, AZ", iv
    assert turn["audit_ready"] is True, turn

    # 3. Readiness rules for both branches.
    assert interview.audit_ready({"track": "new",
                                  "business": {"city": "Mesa, AZ", "category": "pilates studio"}}) is True
    assert interview.audit_ready({"track": "new",
                                  "business": {"city": "Mesa, AZ"}}) is False   # nothing to scan
    assert interview.audit_ready({"track": "existing",
                                  "business": {"name": "X", "city": "Mesa"}}) is True
    assert interview.audit_ready({"track": "existing",
                                  "business": {"city": "Mesa"}}) is False       # no subject
    assert interview.audit_ready({"business": {"name": "X", "city": "Mesa"}}) is False  # no track

    # 4. Extraction never raises on garbage.
    assert interview.extract([{"role": "user", "content": "hi"}],
                             chat_fn=lambda *a, **k: "not json at all")["track"] is None

    print("interview OK: greeting, chat turn, extraction, readiness branches, garbage-safe extract")


if __name__ == "__main__":
    demo()
