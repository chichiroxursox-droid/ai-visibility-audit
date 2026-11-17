"""Onboarding interview — the CHEAP half of the two-model split.

A small, warm "honest consultant" intake that runs on a cheap model (Groq),
distilled from bizplan-builder/workflows/01-interview.md. It has two jobs:
  1. Chat: ask one question at a time, forking on Q0 (new / existing / existing+new).
  2. Extract: turn the transcript into the structured `interview` dict that decides
     whether we run the full audit (existing) or a market landscape scan (new).

The capable model (Opus, in tools/score.py) does the expensive synthesis/plan work
later — this module never touches it.

Stateless by design: the caller (api /chat) holds the transcript and passes it in;
we inject the system prompt. The model client is injectable (`chat_fn`) so the flow
is testable with no network/API spend — see tools/test_interview.py.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from tools import common

INTERVIEW_MODEL = os.environ.get("INTERVIEW_MODEL", "llama-3.3-70b-versatile")

# (messages, json_mode) -> content string
ChatFn = Callable[..., str]

GREETING = (
    "Hey — I help local businesses get found online, whether on Google or in AI "
    "assistants like ChatGPT. To point you the right way, one quick question first: "
    "is this a business you already run, an idea you're thinking about starting, or an "
    "existing business you want to add a new service to?"
)

SYSTEM = (
    "You are a warm, honest small-business consultant running a SHORT intake chat. Not a "
    "cheerleader — a straight-talking advisor. Ask ONE question at a time, in plain language. "
    "Keep each reply to a couple of sentences. Accept rough ranges and 'not sure' without "
    "pushing; never interrogate.\n\n"
    "Q0 (already asked in the greeting) forks everything: NEW idea / EXISTING business / "
    "EXISTING + new offering. If it's not yet clear from the conversation, pin it down first.\n\n"
    "Your FIRST priority is the identity we need to look them up online, gathered naturally:\n"
    "  - business name (for a new idea, a working name is fine)\n"
    "  - city or service area\n"
    "  - what they actually do (the trade/service, e.g. 'brake repair', 'basketball lessons')\n"
    "  - their website or Google listing URL, if they have one — if they give a site, tell them "
    "you'll pull details from it so they don't have to repeat themselves.\n\n"
    "Once you have identity + track, get a feel for a few levers, most important first — but only "
    "the ones they haven't already told you: the 3-years-out outcome ('what would make you say this "
    "worked?'), roughly what they charge and how busy they are, and THE key question — do they ever "
    "turn people away or run a waitlist (capacity-bound) or do they need more customers "
    "(demand-bound)? For an existing business, a rough monthly take-home is gold. For a new idea, "
    "ask who the customer is and the one assumption that sinks it if wrong.\n\n"
    "Recap what you've heard now and then so they can correct you. When you have the identity, the "
    "track, and a rough sense of the outcome, tell them you have enough to run their visibility "
    "audit / market scan and that they can hit the button when ready. Do NOT output JSON or lists of "
    "fields — just talk like a person."
)

EXTRACT_SYSTEM = (
    "You extract structured facts from a business intake conversation. Output ONLY a single JSON "
    "object, nothing else. Use null for anything not clearly stated — never guess or invent. "
    "Map the track to exactly one of: \"new\", \"existing\", \"existing_plus_new\", or null.\n"
    "Shape:\n"
    "{\"track\": <str|null>, \"business\": {\"name\": <str|null>, \"city\": <str|null>, "
    "\"category\": <str|null>, \"website\": <str|null>}, \"outcome\": <str|null>, "
    "\"levers\": {<any economic facts stated, e.g. price, volume, constraint, monthly_takehome>}, "
    "\"notes\": <str|null>}"
)
EXTRACT_USER = "Extract the fields from this conversation:\n\n"


# --- schema -------------------------------------------------------------------

class BusinessId(BaseModel):
    name: Optional[str] = None
    city: Optional[str] = None
    category: Optional[str] = None
    website: Optional[str] = None


class Interview(BaseModel):
    track: Optional[str] = None          # new | existing | existing_plus_new
    business: BusinessId = Field(default_factory=BusinessId)
    outcome: Optional[str] = None
    levers: dict = Field(default_factory=dict)
    notes: Optional[str] = None


def _empty() -> dict[str, Any]:
    return Interview().model_dump()


# --- readiness ----------------------------------------------------------------

def audit_ready(interview: dict[str, Any]) -> bool:
    """True once we have enough to run something real.

    existing / existing+new: need a name + city (the engine infers the category from
    the name when Google's is generic — see common.best_category).
    new: there's no subject to self-audit, so we need city + a category to scan the market.
    """
    b = interview.get("business") or {}
    track = interview.get("track")
    city = (b.get("city") or "").strip()
    if not track or not city:
        return False
    if track == "new":
        return bool((b.get("category") or "").strip())
    return bool((b.get("name") or "").strip())


# --- model plumbing (cheap backend = Groq) ------------------------------------

def _groq_chat(messages: list[dict], json_mode: bool = False,
               temperature: float = 0.5, max_tokens: int = 1024) -> str:
    from groq import Groq  # lazy — importing this module shouldn't require the SDK
    client = Groq(api_key=common.get_key("groq", required=True))
    kwargs: dict[str, Any] = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(
        model=INTERVIEW_MODEL, messages=messages,
        temperature=temperature, max_tokens=max_tokens, **kwargs,
    )
    return resp.choices[0].message.content or ""


def _loads(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        i, j = raw.find("{"), raw.rfind("}")
        if i == -1 or j == -1:
            raise
        return json.loads(raw[i:j + 1])


def _transcript(history: list[dict]) -> str:
    role = {"user": "Owner", "assistant": "Consultant"}
    return "\n".join(f"{role.get(m['role'], m['role'])}: {m['content']}" for m in history)


# --- public API ---------------------------------------------------------------

def extract(history: list[dict], chat_fn: Optional[ChatFn] = None) -> dict[str, Any]:
    """Pull the structured interview dict from a transcript. Never raises — a bad
    extraction returns the empty shape so the chat keeps working."""
    chat_fn = chat_fn or _groq_chat
    try:
        raw = chat_fn(
            [{"role": "system", "content": EXTRACT_SYSTEM},
             {"role": "user", "content": EXTRACT_USER + _transcript(history)}],
            json_mode=True, temperature=0.0, max_tokens=600,
        )
        return Interview.model_validate(_loads(raw)).model_dump()
    except Exception:  # noqa: BLE001 — extraction is best-effort, chat must survive it
        return _empty()


def step(history: list[dict], chat_fn: Optional[ChatFn] = None) -> dict[str, Any]:
    """One chat turn. `history` is the prior [{role, content}] WITHOUT the system prompt.
    Returns {reply, interview, audit_ready}."""
    chat_fn = chat_fn or _groq_chat
    if not history:
        return {"reply": GREETING, "interview": _empty(), "audit_ready": False}
    reply = chat_fn(
        [{"role": "system", "content": SYSTEM}] + history,
        json_mode=False, temperature=0.6, max_tokens=400,
    )
    convo = history + [{"role": "assistant", "content": reply}]
    interview = extract(convo, chat_fn)
    return {"reply": reply, "interview": interview, "audit_ready": audit_ready(interview)}
