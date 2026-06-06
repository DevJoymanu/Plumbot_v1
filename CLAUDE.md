# Plumbot – Claude Code Instructions

## Project Overview
Plumbot is a WhatsApp-based appointment scheduling and sales chatbot for Homebase Plumbers in Harare, Zimbabwe. It is built with Django, deployed on Railway, uses Twilio for WhatsApp messaging, and DeepSeek API for AI-powered intent classification and response generation.

## Core Files
- `whatsapp_webhook.py` / `views.py` — main conversation flow logic
- `send_followups.py` — Railway cron job for follow-up scheduling
- DeepSeek API integration — intent classification and response generation

## Coding Rules
- Never introduce new dependencies unless explicitly asked
- Reuse existing infrastructure and patterns already in the codebase
- Always preserve WAMID deduplication logic — never remove it
- Exit-signal detection must always run before any flow-stage logic
- Never re-pitch the site visit to a customer who has already committed

## Conversation Flow Logic
Plumbot uses Hormozi's four-stage qualification framework:
1. **Value** — lead with what we offer and why it matters
2. **Price** — be upfront about pricing before heavy qualification
3. **Qualification** — ask targeted questions using "this or that" framing
4. **Close** — use presumptive closes and micro-yes ladders

When editing flow logic:
- Customers may respond with partial answers (e.g. just a day name like "Sunday") — always handle fuzzy/partial date-time inputs gracefully
- Support both English and Shona responses
- Avoid bot loops — if a question has already been asked, do not repeat it
- Use the semantic duplicate question detector before sending any qualification question

## DeepSeek API Integration
The DeepSeek API is used for intent classification and response generation. When improving prompts or API calls:
- Embed step-by-step reasoning instructions in the system prompt
- Instruct the model to identify customer intent before selecting a response
- Use chain-of-thought style prompting: interpret → consider alternatives → select stage → respond
- Keep responses short, warm, and conversational — like a knowledgeable colleague texting

## System Prompt for DeepSeek
When generating or editing the DeepSeek system prompt, use this as the base:

---
You are Plumbot, a WhatsApp sales and scheduling assistant for Homebase Plumbers in Harare, Zimbabwe. Before every response, reason through the following steps internally:

1. **Intent** — What is the customer actually asking or signaling? Look beyond the literal words.
2. **Stage** — Which of the four stages are they in: value, price, qualification, or close?
3. **Ambiguity** — Is their message unclear or partial (e.g. just a day name, a one-word reply)? If so, clarify gently without repeating yourself.
4. **Commitment signals** — Are they showing readiness to book? If yes, move to close immediately.
5. **Exit signals** — Are they trying to leave the conversation? If yes, acknowledge gracefully and leave the door open.

Then respond:
- In the same language they used (English or Shona)
- Warmly and conversationally — never robotic
- Concisely — WhatsApp messages, not essays
- With presumptive framing — offer choices, not yes/no questions
- Leading with value and confidence, not desperation
---

## Common Bugs to Watch For
- Bot re-pitching site visit after customer already agreed → check commitment state before sending pitch
- Price queries falling through to wrong flow stage → classify price intent before stage routing
- Duplicate messages → always check WAMID before processing
- Follow-up cron skipping eligible leads → check lead eligibility filter logic carefully
- Flow not advancing on partial date inputs → normalise day names to full date-time before validation

## Current State

This section is context for the next session. It explains how the **WhatsApp quoted-reply ("highlighted message") feature** works end to end, what's solid, what's fragile, and the patterns to preserve. Read it before touching the conversation/send pipeline.

### What "reading highlighted texts" means here
On WhatsApp, when a customer long-presses one of the bot's messages and replies to it, the grey quoted snippet is a *reply context*. The Twilio/Meta Cloud API delivers this as a `context` object on the inbound message that contains **only the WAMID** of the quoted message (`context.id`) — **never the quoted text itself**. So the bot can't "read" the highlight directly; it must map that WAMID back to text it already stored. The whole feature is built around that constraint.

### Files involved and what each part does

**`bot/models.py`** — `Appointment` conversation helpers (the storage + resolution layer). `conversation_history` is a `JSONField` list of `{role, content, timestamp}` dicts; the feature adds three optional keys to entries (`message_id`, `quoted`, `media_index`) with **no migration** (JSONField is schemaless).
- `add_conversation_message(role, content, message_id=None, quoted=None)` — logs a turn and stores the WAMID + the resolved quoted text. The back-to-back duplicate guard now *back-fills* `message_id`/`quoted` onto an existing identical entry instead of dropping them (matters because the webhook logs the user turn on arrival and `generate_response` re-logs it).
- `attach_message_id(role, content, message_id)` — stamps an outbound WAMID onto an already-logged entry, matching the most recent role+content entry that has no WAMID yet. Needed because we log the assistant reply *before* we send it (the WAMID only exists after the send returns).
- `record_sent_media(media_map, summary)` — logs one transcript entry for a batch of sent images carrying a `media_index` of `{wamid: description}`, so replies that quote a specific photo resolve to what that photo shows (without bloating history with one entry per image).
- `resolve_quoted_message(message_id)` — turns an inbound `context.id` into text: scans history newest-first for a matching `message_id`, then checks each entry's `media_index`. Returns `None` when the quoted message predates the feature or was sent through a non-stamping path.

**`bot/whatsapp_webhook.py`** — the ingestion + send pipeline (where WAMIDs are captured and resolved).
- `process_message_change` extracts `quoted_id = (message.get('context') or {}).get('id')` and passes it to `handle_text_message`.
- `handle_text_message(sender, text_data, message_id, quoted_id)` resolves `quoted_text` via `appointment.resolve_quoted_message`, logs the user turn with `message_id` + `quoted`, prints a `🔗` diagnostic line, and forwards `quoted_text` into the batch.
- `_enqueue_for_response` / `_flush_text_batch` — the per-sender debounce batch now carries a 3-tuple `(message_body, message_id, quoted_text)`; on flush it uses the **last non-empty** quote in the batch (the customer's most recent reply target).
- `_generate_and_schedule_reply` passes `quoted_context=quoted_text` into `plumbot.generate_response`.
- `delayed_response` captures the result of `send_text_message`, pulls `sent_wamid` from `messages[0].id`, and calls `attach_message_id("assistant", reply, sent_wamid)` after the send. **This is the main place outbound text WAMIDs get stored.**
- `send_previous_work_photos` captures each image's WAMID into a `media_index` and calls `record_sent_media(...)`. **This is where outbound image WAMIDs get stored.**

**`bot/views/plumbot/response_mixin.py`** — where the quote reaches the LLM.
- `generate_response(..., quoted_context=None)` threads the quote to its three `generate_contextual_response` calls.
- `generate_contextual_response(..., quoted_context=None)` → `_generate_retry_response(..., quoted_context=None)`, which injects a "THE CUSTOMER IS REPLYING TO THIS EARLIER MESSAGE OF YOURS: …" block into the DeepSeek prompt so references like "this one"/"the first" resolve.
- **Changed most recently (this session):** the standalone/service-inquiry branch in `generate_response` now builds `_q_msg` (the raw message augmented with `[Customer is replying to: "…"]`) and feeds it to `detect_service_inquiry`, `handle_service_inquiry`, and `_answer_standalone_question`. This is what makes "how much is this?" work when it's a reply to a portfolio photo or price line — that path previously never saw the quote, which is why the feature appeared to "not work" for the most common case. `incoming_message` stays **raw** for the rule-based checks; only the LLM-facing calls get `_q_msg`.

### Completed and working
- WAMID storage for outbound **text** (via `delayed_response`) and outbound **images** (via `record_sent_media`).
- Inbound `context.id` extraction → resolution to text/description.
- Quote injected into the DeepSeek prompt on the **retry-rephrase** path and the **standalone/service-inquiry** path.
- Resolution logic verified in isolation (Django shell): text quote → text, image quote → description, unknown WAMID → `None`.

### Partially done / fragile (do not assume these are covered)
- **Injection coverage is not universal.** The quote reaches the LLM only in the retry path and the standalone/service-inquiry path. It is **not** passed into: the availability/date classifier (`_classify_availability_response` / `_handle_availability_date_response` — so replying to a "Tuesday or Thursday?" offer with "the first" won't carry the quote into date classification), the FAQ layer, multi-intent compose, or the booking path. Extending coverage means threading `quoted_context` into those specific handlers the same way.
- **Outbound stamping only happens on two paths.** Replies that quote a message sent via a *direct* `send_text_message` call (plumber alerts, the photos intro line, and the `generate_photo_followup` text) will resolve to `None` because those sends don't stamp a WAMID. If a quote "isn't recognised," check whether the quoted message was sent through a stamping path.
- **Resolution is best-effort.** Messages predating the feature, or in a different appointment's history, return `None`; the bot then silently behaves as if there were no quote. The `🔗 Reply to message <id> — not found in history` log line is the signature of this.
- **`attach_message_id` matches by exact content.** Two identical bot replies are disambiguated only by "most recent without a WAMID" — usually correct, but a possible mis-stamp edge case.
- **Windows-local gotcha:** `add_conversation_message` (and other handlers) `print()` emoji; on a Windows cp1252 console this raises `UnicodeEncodeError`, and `add_conversation_message` *re-raises*. Harmless on Railway (UTF-8 stdout) but it will crash local shell/test runs unless you set `PYTHONIOENCODING=utf-8`.
- **Not verified against live WhatsApp this session** — only unit-level. End-to-end confirmation should use the Railway `🔗` log lines.
- **Unrelated in-flight edits in the working tree:** `bot/portfolio_catalog.py`, plus a gallery-caption change in `send_previous_work_photos` and a price-disclaimer guard in `response_mixin.py` (`'$' not in reply → skip disclaimer`). These came from a parallel session and are unrelated to the quote feature; don't bundle them blindly.

### Key architecture decisions / patterns to preserve
- **WAMID round-trip, not text capture.** Because the API gives only `context.id`, the design stores WAMIDs on history entries and resolves locally. Keep storing outbound WAMIDs whenever you add a new send path, or quotes to those messages silently break.
- **Keep the quote out of the rule engine.** The state machine (acks, date parsing, dedupe, keyword classifiers) runs on **raw** `incoming_message`. The quote is a *separate* `quoted_context`/`_q_msg` that only ever reaches LLM/classification calls. Folding the quote into `incoming_message` globally would corrupt date parsing, ack detection (`.strip().lower() in acks`), and pricing-keyword scans — that's why it's threaded as its own parameter.
- **Stamp-after-send / back-fill.** Assistant replies are logged before the (1–5 min delayed) send; the WAMID is back-filled by `attach_message_id` once the send returns. The duplicate-guard back-fill in `add_conversation_message` exists so the arrival-time metadata survives `generate_response`'s re-log of the same turn.
- **Schemaless extension of `conversation_history`.** New per-turn metadata is added as optional JSON keys, never new columns — consistent with how the rest of the transcript is stored, and avoids migrations.
- **Batch carries the quote.** The debounce accumulator threads `quoted_text` through and uses the last non-empty one, so rapid-fire replies still attribute the quote to the right (latest) message.
- All new parameters are **optional with safe defaults** (`None`), so every existing caller and the WAMID-dedup logic keep working untouched — preserve that when extending.
