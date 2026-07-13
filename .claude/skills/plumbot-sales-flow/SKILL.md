---
name: plumbot-sales-flow
description: The sales-process and conversation-flow rulebook for Plumbot (Homebase Plumbers WhatsApp bot). Use this skill BEFORE editing anything the customer reads or any code that decides what the bot says — sales copy, pricing replies, qualification questions, closers, follow-up messages, objection/delay/exit handling, intent routing in whatsapp_webhook.py, response_mixin.py, out_of_scope_handler.py, extraction/booking flow, or the DeepSeek prompts. Consult it even for "small" copy tweaks or one-line bug fixes in these files — most regressions in this codebase came from edits that didn't know these rules. Also use it when designing follow-up cadence, reviewing conversation transcripts for drop-off, or onboarding a new plumbing tenant.
---

# Plumbot Sales Flow Rules

This skill encodes the sales process for Plumbot. Every rule here exists because
breaking it lost a real lead or caused a production bug — the "why" is included
so you can apply the spirit, not just the letter, to situations the rule didn't
anticipate.

**Read first, always:**
- `bot/sales_profiles/homebase.md` — the ONLY source of truth for prices,
  services, hours, USPs, and objection answers. Never invent or "remember" a
  figure; quote "from" prices and defer exact numbers to the free on-site visit.
- The "Current State" section of `CLAUDE.md` for architecture context.
- Before writing or changing any customer-facing copy, read
  `references/corrected-examples.md` — real production exchanges (bad reply →
  corrected reply, with outcomes) for each rule below. The conv 566 vs 658 pair
  shows the same message ghosting under the old code and booking under the
  corrected script.

For deep design work (rewriting copy, new qualification sequences, drop-off
diagnosis), delegate to the `plumbing-sales-strategist` agent — this skill is
the guardrail set; that agent is the copywriter.

## The frame: Hormozi's four stages

Every conversation moves Value → Price → Qualification → Close.

1. **Value** — lead with what we offer and why it matters. USPs: free on-site
   visit + fixed quote on the spot, fixed price up front, licensed, satisfaction
   guaranteed.
2. **Price** — be upfront when asked, never before (see pricing gates below).
3. **Qualification** — targeted "this or that" questions, one at a time, never
   an interrogation. Micro-yes ladder: stack small agreements.
4. **Close** — presumptive framing ("What works better for you, morning or
   afternoon?"), never yes/no asks. Never re-pitch the visit to someone who
   already committed.

Commitment signals jump straight to Close. Exit signals are detected before any
stage logic runs — always.

## Pricing gates (the most-regressed area)

**Never volunteer price.** If the customer didn't explicitly ask for a price,
the reply must not contain one — answer what they asked, then progress.
Volunteering price skips the Value stage and anchors on cost before they're
bought in. `ResponseMixin._should_volunteer_pricing(intent, message,
price_requested)` is THE single gate: any new code path that could emit a price
must call it, never re-check intent confidence on its own. Naming a fixture
with no question ("I want a shower cubicle") is a buying/scope statement — it
gets captured and the flow advances; it is not a price ask.

**"A quote" is not a price ask.** Quote/quotation requests lean to the free
site visit (`_build_job_quote_reply` — acknowledge, no figures, pitch the
visit). Only an explicit price-figure ask — how much / price / cost / rate,
Shona "marii" / "mutengo" — gets approximate chat prices. The split lives in
`_asks_price_figure` (excludes 'quote') vs `_asks_for_quote`. The owner wants
quote-seekers at the visit, where the real quote closes the sale. The quote
pitch is sent ONCE — a second job-shaped message gets the scripted next
question, never the identical pitch again (`_already_sent_job_quote_pitch`).

**When you do price:**
- Break down supply + install (figures from `_FAMILY_LABOUR_BREAKDOWN`).
- Disclaimer wording: "These are approximate starting prices — your exact quote
  is confirmed once the plumber sees the space." No "site visit" phrasing.
- Close with the budget tie-down `_price_tiedown` ("That sit alright with your
  budget?") — never an open "What did you have in mind?". After price comes
  Qualify, and a yes here hands straight off to the visit close.
- New product price replies route their closer through `_product_price_close`
  or `_get_pricing_followup_prompt` — never hardcode an open question.

**Budget objection = reframe, never negotiate.** A "no" to the budget tie-down
gets the all-in reframe ("That's everything in — supply, install, fully fitted,
no extras on the day...") and an offer of the exact number for their space.
Never discount, never ask their budget figure (that flow was removed on
purpose — don't reintroduce it).

## The one recurring bug: customer's words override gates

This bug shipped at least six times in six different code paths: a gate or
pending flow state (carried-over LLM intent, `delay_timeframe` hold,
`delay_email` hold, duplicate-question gate) swallowed what the customer just
said. The rule: **a real signal in the current message always wins** — a named
product, an explicit price ask, an elliptical "this one?" on a quoted photo, a
buying statement, an exit/delay signal, a bare "No" that means "nothing else,
proceed".

Use the shared deterministic resolvers instead of adding LLM round-trips or new
local checks:

| Resolver | Where | Guards against |
|---|---|---|
| `_should_volunteer_pricing` | response_mixin.py | any unprompted price |
| `_asks_price_figure` / `_asks_for_quote` | response_mixin.py | quote vs price confusion |
| `_correct_service_intent` | response_mixin.py | LLM product mis-mapping |
| `_is_carryover_pricing` / `_is_unprompted_carryover_pricing` | response_mixin.py / whatsapp_webhook.py | stale intent pricing an area reply |
| `_is_quoted_item_reference` | whatsapp_webhook.py | "this one how much?" on a photo |
| `_delay_breakout_inquiry` | out_of_scope_handler.py | delay hold swallowing a question — wire EVERY pending state to it |
| `_is_purchase_commitment` | response_mixin.py | buying statement leaking price/size |
| `_is_job_quote_request` / `_names_multiple_products` | response_mixin.py | job description mistaken for price ask |
| `wants_whatsapp_delivery` | out_of_scope_handler.py | "use this chat" re-asked for email |

Prefer deterministic resolvers for short/fuzzy strings; reserve DeepSeek for
genuinely ambiguous language. (Exception, user-approved: the budget-decline
classifiers are AI-primary with keyword fallback.)

## Copy rules

- **No emojis in customer-facing copy.** Ever. Logs and dashboards are fine.
- **Mirror the customer's language** — English or Shona, per message.
- **Short, warm, conversational** — a knowledgeable colleague texting. Never
  robotic, never desperate, no essays.
- **Script first, vary on retry.** The FIRST ask of every early-flow question
  uses the exact approved script (hardcoded, no LLM); paraphrase only on a
  repeat/re-ask. Consistency on first contact converts; variation is a retry
  tactic. New first-contact responses: hardcode the first pass, reserve the
  LLM for retries.
- **Casual visit copy.** The assessment is "a quick look at the bathroom — 20
  minutes or so", never a formal "free on-site assessment" pitch. Repeated
  formal pitching repels leads.
- **Don't re-ask what you have.** A named day means ask only for the time; ask
  for the day only when the timeframe is vague. Never repeat a question already
  asked (semantic duplicate detector runs before qualification questions).
- **Two-message split.** An acknowledgement + scripted question in one block
  reads pre-meditated. Split via `MESSAGE_SPLIT_MARKER` (ack, then question,
  short human gap); the split reply must flow through the main dispatcher, not
  a direct `send_text_message` (WAMID stamping per part).
- **Answer, then tie-down.** After answering ANY lead question: answer → one
  soft micro-yes tie-down → the next booking field only once they engage.
  Never answer + interrogate in one message. Chokepoints:
  `_get_pricing_followup_prompt` and `_next_forward_question`, gated by
  `_last_assistant_was_tiedown()`. Price replies close on the budget tie-down;
  non-price on `_yes_tiedown` (context-aware: property-scope question once a
  job is on the table, "What are you looking to get sorted?" on a cold opener).
  Never stack two questions in one reply.
- The plumber is **Takudzwa** (+263774819901) everywhere — one prior fix
  introduced a second name in adjacent turns.

## Objection & exit playbook

Exit-signal detection runs before all flow logic. Never fight an exit — but
never end with nothing in their hands either:

- **Soft brush-off** → one value-add attempt: offer the portfolio + pricing PDF
  by email, capture the address (`brush_off` branch of `_build_delay_reply`).
  Lead is parked, not dropped — parked nudges re-engage at 3 and 7 days.
- **"Let me get other quotes"** → agree, reframe the comparison axis: send the
  portfolio on WhatsApp so they weigh quality not just price, arm them to
  compare like-for-like ("check the others are all-in and guarantee the work"),
  then ask their timeframe (funnels into `delay_timeframe`).
- **Self-initiated defer** ("I'll get in touch over the weekend") → park
  gracefully with a check-back date; do NOT pressure for a slot even if the
  timeframe is near (`_is_self_initiated_defer` gates the booking pivot).
- Never fabricate scarcity or urgency — honest slot availability only.

## Domain facts that keep getting wrong

- **Service area is Zimbabwe-wide.** Homebase is mobile. Decline ONLY: Gweru,
  Bulawayo, Mutare, Masvingo, Victoria Falls, Hwange, Beitbridge, Plumtree.
  Distance ≠ out of area; the decline reply says the specific place is too far,
  never "Harare only".
- **A corner tub is a built-in tub** — from US$160 all-in, NOT the
  freestanding US$670. (Pricing rule only; for sizes, corner is its own block.)
- **A wall-mounted / wall-hung toilet is the chamber install** — from US$160
  all-in (supply US$130 + install US$30), NOT toilet-seat pricing (US$70).
  Deterministic resolver: `_mentions_wall_hung_toilet` → `wall_hung_toilet`
  intent (prod: "wall mounted toilet system" was quoted the seat block).
- Hours: Sunday–Friday 8am–6pm, closed Saturday. Payment: cash, EcoCash, bank
  transfer — no deposit language without owner sign-off.

## Process requirements (non-negotiable)

1. **Any change to intent classification, pricing gates, or flow routing adds
   an API-free case to `tests/test_bot_responses.py` TEST 0** — enforced by
   the pre-commit gate (`PLUMBOT_GATE=1 python tests/test_bot_responses.py`).
   If a TEST 0 case calls a helper that uses other ResponseMixin methods, the
   fake-self must expose them.
2. **Every production bug gets a scenario file** in `scenarios/` (expect:/
   reject: lines; see `scenarios/quote_pitch_no_repeat.txt` for the format).
   Run live with `python manage.py run_scenarios`; sanity-check interactively
   with `python manage.py chat`. If the bug was a tone/judgment failure, also
   add a distilled before/after pair to `references/corrected-examples.md`.
3. On Windows, set `PYTHONIOENCODING=utf-8` before running tests locally.
4. New per-turn metadata = optional JSON keys on `conversation_history`, never
   model columns. New handler params default to `None`.
5. Any new outbound send path must stamp its WAMID (`attach_message_id` /
   `record_sent_media`) or quoted-reply resolution silently breaks.
6. End every edit with a suggested `git commit -m` message.
