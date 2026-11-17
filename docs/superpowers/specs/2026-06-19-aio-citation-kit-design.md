# Spec: AIO-Citation Kit (MVP #2, part 1)

- **Date:** 2026-06-19
- **Status:** Design (approved to build the deterministic core first — see build order §end).
- **Parent:** `docs/superpowers/specs/2026-06-18-delivery-engine-design.md` (this is the first buildable,
  zero-customer-keys slice of the delivery engine — the "make AI recommend you" wedge).
- **How produced:** multi-agent research + adversarial reality-check workflow (web-verified to June 2026),
  synthesized into this implementation brief. Every correction/compliance flag from the skeptic passes
  is baked in. Notable corrections vs. naive assumptions: FAQ rich results removed (May 7 2026);
  skip llms.txt + speakable; Apple no longer requires D-U-N-S; Google verification is video-first;
  never collapse the 3 AI engines into one metric.

---

# AIO-Citation Kit — Implementation Brief

**Input contract:** one `audit.json` per business. **Output contract:** 3 deliverables (Directory-Gap
List, FAQ Content, JSON-LD Schema) + a re-audit proof hook.

Read §6 first — it is the do-not-build list that keeps us out of ToS trouble.

## 0. Architecture at a glance

```
audit.json ──> [parse + validate] ──> business profile object
                     │
        ┌────────────┼─────────────────────────────┐
        ▼            ▼                               ▼
  Deliverable 1   Deliverable 2                 Deliverable 3
  Directory-Gap   FAQ Content                   JSON-LD Schema
  (det. code +    (LLM draft +                  (det. code +
   curated map)    HUMAN GATE)                   validator gate)
        │            │                               │
        └────────────┴───────────────┬───────────────┘
                                      ▼
                          Spam/NAP/Schema-mismatch checks
                                      ▼
                          Operator review → publish
                                      ▼
                          [Proof hook: re-audit harness]
```

Four automation classes: **`deterministic_code`** (pure transform), **`curated_data`** (hand-maintained
lookups we own), **`llm_generated`** (model drafts, never auto-published), **`human_gate`** (a person
approves before output leaves the kit).

## 1. The 3 deliverables

**D1 — Directory-Gap List.** Ranked "get listed here" report (machine `gap_list.json` + human Markdown).
Per row: `platform`, `category` (review/directory/social/trade_specific/aggregator_readonly),
`cited_by_engines[]`, `claim_url`, `verification_method`, `effort_minutes`, `free_or_paid`, `claimable`,
`status`, `priority_tier`, one-line action. Sections: Claim-Free-First, Trade-Specific, Consider-Paid,
Participation-Strategy (Reddit/FB/Nextdoor — NOT claims), Not-Claimable (Carfax/Wikipedia). Feeds from
`aio.engines[*].cited_domains[]`, `all_cited_domains[]`, `business.*`, `competitors[].website`.
**deterministic_code over curated_data.** No LLM in core path.

**D2 — FAQ Content.** 12–18 Q&A pairs tailored to trade+city+services; each answer a **40–60-word,
conclusion-first, self-contained** block. Outputs `faq_draft.json` (status `pending_review`) → SME
review doc → (post-approval) FAQPage JSON-LD. Feeds from category, city, `service_pages[]`, `reviews`
(mine real questions), `has_faq_schema` (augment, don't replace). **llm_generated → mandatory human_gate.**

**D3 — JSON-LD Schema.** Validated `<script type=application/ld+json>` blocks: `LocalBusiness` (specific
subtype), `Organization`, conditional `Service`/`AggregateRating`/`OpeningHoursSpecification`, and
(post-approval) `FAQPage`. Plus a compliance report (required-fields, NAP consistency, schema-vs-visible
warnings, validator result). **deterministic_code + curated_data, gated by a validator that fails loudly.**

## 2. Directory-Gap algorithm

Audit data is ground truth; when audit and the curated map disagree, **audit wins**.

**Step 0 — `directories.json` (curated, ~40 entries).** Per entry: domain, display_name, category,
claimable, claim_url, verification, effort_minutes, free, cited_by_default[], trade_specific_for[],
requirements[], notes. **2026 facts baked in:** Apple Business (Apr 14 2026) accepts EIN/Federal Taxpayer
ID (no D-U-N-S gate); Google = video verification primary (postcard deprecated); Angi only (HomeAdvisor
redirects), paid; Yelp phone-PIN ~24h; **Bing Places carries real weight (Microsoft Copilot pulls from
it)**; BBB free; Carfax = `aggregator_readonly`, not claimable; Reddit = `social`, high-value AI channel
(Perplexity cites Reddit ~46.7%) → authentic participation, not a claim task.

**Step 1 (det):** union `cited_domains[]`, normalize (strip www, registrable domain), count **per-engine**
frequency. Never collapse ChatGPT+Perplexity (11% overlap, 46× brand-citation gap).
**Step 2 (det):** status = claimed / cited-but-unclaimed (**highest ROI — AI already trusts it**) /
unclaimed / not-claimable. Empty `claimed_on` → treat claimable as unclaimed (don't assume).
**Step 3 (curated + optional LLM w/ gate):** trade-specific detection via `trade -> [dirs]` map; unknown
domains optionally LLM-classified, **agency approves before priority list**.
**Step 4 (det) ranking:** `score = citation_frequency*3 + (cited_but_unclaimed?5:0) + (trade_specific?4:0)
+ free_bonus - (not_claimable?999:0)`. Bucket into sections; paid items get an ROI note, never buried.
**Step 5 (det):** blocker pre-check — flag `requirements[]` vs audit data upfront (e.g. "Apple needs EIN").

## 3. Schema spec — generate vs SKIP

**JSON-LD only (no Microdata). One instance of each type per page.**

GENERATE: **(A)** `LocalBusiness` specific subtype (AutoRepair/Plumber/PressureWashingService…) — `name`
+ discrete `PostalAddress` (parse into fields, never one string); recommended `telephone`/`url`/`geo`/
`areaServed`/`priceRange` only if present; mobile-only → omit streetAddress, use areaServed. **(B)**
`Organization` (name, url, knowsAbout[], sameAs, @id cross-ref). **(C)** `sameAs` — **only
verified/claimed profiles**; never publish inferred URLs (those go in D1 as "recommend to claim");
validate each resolves. **(D)** `AggregateRating` nested, **conditional** on real reviews — omit if
empty (fabrication = policy violation). **(E)** `OpeningHoursSpecification` conditional on real hours.
**(F)** `Service` conditional (service trades). **(G)** `FAQPage` — llm + human gate; **rich results
removed May 7 2026**, still parsed by Perplexity/Copilot/AI-Overviews; uplift **mixed/unproven** — build
for structure, NOT as a promised lever; only emit if Q&A is **visible on the page** (schema-only =
cloaking). **(H)** `Article`+`citation` conditional on real pages/sources.

SKIP: `speakable` (bogus 60% claim, minimal local voice traffic), `llms.txt` (~10% adoption, no proven
lift, revisit 2027), Microdata (always), `priceRange` unless real.

**Validator gate (det, fails loudly):** JSON syntax → required-fields → (Rich Results Test if key) →
**schema-vs-visible-content (mismatch = cloaking = block)** → **NAP char-for-char vs audit**. Emits
`Schema Valid ✓` or a specific actionable error. Broken/fabricated schema never ships.

## 4. FAQ / content rules

Tailor with audit data (never generic placeholders). Extractable answer = 40–60 words, conclusion-first,
self-contained, concrete data **only if audit-backed** (44.2% of LLM citations come from the first 30%
of text → front-load). Source questions from real `reviews`; templates (`assets/faq-templates.json` by
category) as scaffolding; competitors for ideas only. Augment existing FAQs, don't overwrite. Keep human
readability + E-E-A-T (no bot-speak).

**Human fact-check gate (MANDATORY):** no FAQ/AI content is published or embedded in schema without a
logged SME approval (name + timestamp → `audit_log.json`); publish path blocks without it. Never generate
"typical cost $X", "24/7", "licensed and insured" unless audit/business confirms it. Scaled templated FAQ
across businesses without per-business facts = scaled-content-abuse signal (SpamBrain, 50–80% drops).

## 5. Proof hook — citation-lift via re-audit

Query-panel generator (det): 10–30 prompts from category+city (base "best [trade] in [city]" + near-me,
nearby city, broader term, specific service). Tracking harness runs the panel × 3 engines, logs
`{timestamp, engine, query_id, cited_domains[], brand_mentioned, rank, sentiment}`, computes **per-engine
citation_rate**. Delta report: week-over-week, competitor benchmark, source-type breakdown, tied to actions.
**Single-sample caveat (hard rule):** one run = a screenshot, not a metric (40–60% month-to-month
variation, ~4.5-wk half-life). ~30-query weekly baseline for trend; flag N<30 as "trend only"; never
present citation% → $; quarterly re-audits expected.

## 6. Top risks / do-not-build

1. No fabricated structured data (empty > fake). 2. No cloaking (schema must be visible; validator blocks
mismatch). 3. No auto-publishing FAQ/AI content (human gate). 4. No bulk auto-claiming 20+ dirs. 5. No
inferred `sameAs` in published JSON-LD. 6. No keyword stuffing. 7. NAP char-for-char consistent, but
descriptions varied per platform (identical copy = mass-generation tell). 8. Filter `claimable` before
making a claim task. 9. Don't position FAQPage as highest-leverage. 10. No speakable/llms.txt for MVP.
11. Track the 3 engines separately end-to-end. 12. Apple=EIN ok, Google=video, Angi not HomeAdvisor.
13. No bot-speak answer blocks. 14. No permanence/revenue-causation claims (quarterly re-audits expected).

## Build order

1. `assets/directories.json` + Directory-Gap algorithm (highest ROI, fully deterministic, no model risk).
2. JSON-LD generator + loud validator.
3. FAQ generator behind the human gate (`assets/faq-templates.json`).
4. Re-audit proof harness (heaviest; overlaps the AIO multi-sample backlog) — last.

Ship 1–3 as the core kit; 4 is the proof/retention layer.
