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

Orientation for the next session: what the system does today, why it's built this way, and what to watch. Reflects the codebase as of June 2026.

### Architecture
- Django app `bot/` on Railway; WhatsApp via Twilio/Meta Cloud API; all AI via **DeepSeek** through the OpenAI SDK pointed at `api.deepseek.com`.
- The shared DeepSeek client lives in `bot/services/clients.py`; it monkey-patches `chat.completions.create` to **force "thinking" mode off on every call** — thinking mode consumed the `max_tokens` budget and returned empty/truncated JSON, breaking all classifiers (set `DEEPSEEK_THINKING=enabled` to revert).
- `Plumbot` (`bot/views/plumbot/`) is composed from mixins — `state`, `response`, `extraction`, `availability`, `booking`, `reschedule`, `notification`, `plan_upload`; `base.py` wires them and `get_or_create`s the `Appointment` per phone number.

### Inbound pipeline (`bot/whatsapp_webhook.py`)
- `process_message_change` → `handle_text_message(sender, text, message_id, quoted_id)` logs the user turn, resolves any quoted reply, then **debounce-batches per sender** (`_enqueue_for_response` / `_flush_text_batch`) so rapid-fire texts get one answer.
- `_generate_and_schedule_reply` is the router; first step to produce a reply wins, in order: FAQ → unified pre-classifier → STEP 0 multi-intent compose → 0a whole-gallery → 0b specific portfolio piece → 0c portfolio menu → 0d catalogue+prices → 1 photo request → 1b out-of-scope/delay/complaint → 2 service-specific pricing → 3 full pricing overview → 3b repeated-question → 4 normal `generate_response`.
- Outbound is sent on a **1–5 min random delay** via `delayed_response` in a daemon thread; a new inbound message **cancels the pending send** (`_pending_send_events`) so the batch re-runs with the latest context.

### Unified classifier (`bot/unified_classifier.py`)
- One DeepSeek call returns a dict consumed by all downstream handlers (OOS intent, product/service intent, booking-data extraction, photo/repeat/plan-later flags), replacing ~6 separate calls; on failure it returns `None` and callers fall back to their individual classifiers. Accessors: `uc_as_service_inquiry`, `uc_as_oos_classification`, `uc_is_photo_request`, etc.

### Conversation storage (`bot/models.py` — `Appointment`)
- `conversation_history` is a schemaless `JSONField` of `{role, content, timestamp}` dicts plus optional `message_id`/`quoted`/`media_index` keys — transcript metadata never gets migrations.
- Helpers: `add_conversation_message` (logs a turn, back-fills WAMID/quote onto a duplicate entry), `attach_message_id` (stamps an outbound WAMID after send), `record_sent_media` (one entry per image batch carrying `{wamid: description}`), `resolve_quoted_message` (maps inbound `context.id` → stored text/description).

### Quoted-reply ("highlighted message") feature
- WhatsApp delivers only the quoted message's **WAMID** (`context.id`), never its text, so outbound WAMIDs are stamped onto history (text in `delayed_response`, images in `send_previous_work_photos` via `record_sent_media`) and resolved locally on the way back.
- The resolved quote travels as a **separate** `quoted_context` parameter to LLM/classification calls only — never the rule engine. Before STEP 2, `_generate_and_schedule_reply` re-derives the service intent **deterministically** via `_keyword_product_intent` (the customer's own product word wins, else the quoted photo's caption — e.g. a "rain shower" quote → `shower_cubicle`), so "this one how much?" on a portfolio photo prices the quoted item instead of a stale carried-over intent. Deterministic on purpose: the LLM mis-maps short photo captions (a "rain shower" caption was classified `tub_sales`).
- Fragile: coverage is **not universal** (availability/date classifier, FAQ, multi-intent compose, booking path don't receive the quote); only the two stamping send-paths resolve — others return `None` and silently behave quote-less (signature log: `🔗 … not found in history`).

### Portfolio / catalogue (`bot/portfolio_catalog.py`)
- Static list of previous-work pieces (title, "from" price, description, keywords); `match_portfolio_item` returns one item only when the message clearly references a specific piece, else `None` → whole-gallery send. Prices are the business's own "from" rates from `bot/sales_profiles/homebase.md` (source of truth — keep in sync, never invent figures); captions are title-only.

### Pricing & sales (`response_mixin.py`)
- `detect_service_inquiry` → priceable intent; `handle_service_inquiry(intent, message)` builds the reply from a `structured_pricing` table keyed by intent (message used only for language detection). `generate_pricing_overview` gives the full menu; `compose_multi_answer` answers 2+ info questions at once.
- Pricing is gated: don't volunteer price when unasked, don't re-send an already-sent intent, and don't price a message that is a project description / booking-capture answer.

### Booking, availability, scheduling
- `extraction_mixin` pulls fields (service, area, plan status, name, datetime) and tracks `get_next_question_to_ask`; `availability_mixin` checks business-hours slots and suggests alternatives; `booking_mixin` validates completeness and books; `reschedule_mixin` handles AI-detected reschedules; `plan_upload_mixin` handles "I'll send my plan" flows and plan-status nudges; `notification_mixin` alerts the plumber and optionally Google Calendar.

### Follow-ups & cron (`bot/management/commands/`)
- `send_followups` — 4 follow-ups over 18h (0/6/12/18h) for cold leads. Other Railway crons: `send_reminders`, `send_job_reminders`, `notify_priority_leads`, `summarize_unconfirmed_leads`, `process_inbound_emails`.

### Email
- Customer/transactional email goes through the **SendGrid v3 HTTP API (port 443)** in `bot/plumber_notifications.py` (`_send_via_sendgrid`); Railway blocks all outbound SMTP, so the legacy `IPv4SMTPBackend` (`bot/email_backends.py`) is a fallback only. SendGrid click/open tracking is disabled to keep `tel:`/`wa.me` links clean. HTML templates live in `bot/customer_emails.py`; subjects carry `[APT-{id}]` so IMAP replies match back to the appointment.

### Supporting classifiers / safety nets
- `faq.py` (no-API canned facts), `out_of_scope_handler.py` (OOS / delay / complaint), `repeated_question_detector.py` (re-asked questions), `semantic_rescue.py` (rescue unclassifiable messages), `service_type_classifier.py` (bathroom / kitchen / new-install), `services/lead_scoring.py` (lead prioritisation).

### Conventions to follow
- Reuse existing infra; no new dependencies without being asked. Preserve WAMID dedup and exit-signal-first ordering.
- **Keep the quote out of the rule engine** — thread it as `quoted_context`/`_q_msg` to LLM calls only; any new send path must stamp its WAMID or quotes to it break silently.
- New per-turn metadata = optional JSON keys, never new columns; new handler params optional with `None` defaults so existing callers keep working untouched.
- **No emojis in customer-facing copy** (logs/dashboards fine). Support English + Shona.
- **Windows-local gotcha:** handlers `print()` emoji; set `PYTHONIOENCODING=utf-8` or local shell/test runs raise `UnicodeEncodeError` (harmless on Railway's UTF-8 stdout).
- At the end of every edit, provide a suitable `git commit -m` message.
