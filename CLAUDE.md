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

Orientation for the next session: what the system does today, why it's built this way, what's fragile, and the conventions to keep. Reflects the codebase as of June 2026.

### Stack & composition
- Django app `bot/` on Railway; WhatsApp via Twilio/Meta Cloud API; all AI via **DeepSeek** through the OpenAI SDK pointed at `api.deepseek.com`.
- The shared DeepSeek client (`bot/services/clients.py`) monkey-patches `chat.completions.create` to **force "thinking" mode off on every call** — thinking mode ate the `max_tokens` budget and returned empty/truncated JSON, breaking every classifier (`DEEPSEEK_THINKING=enabled` reverts).
- `Plumbot` (`bot/views/plumbot/`) is one class composed from mixins — `state`, `response`, `extraction`, `availability`, `booking`, `reschedule`, `notification`, `plan_upload`; `base.py` wires them and `get_or_create`s the `Appointment` per phone number.

### Inbound pipeline (`bot/whatsapp_webhook.py`)
- `process_message_change` → `handle_text_message(sender, text, message_id, quoted_id)` logs the user turn, resolves any quoted reply, then **debounce-batches per sender** (`_enqueue_for_response` / `_flush_text_batch`) so rapid-fire texts get a single answer.
- `_generate_and_schedule_reply` is the router; the first step to produce a reply wins, in order: FAQ → unified pre-classifier → STEP 0 multi-intent compose → 0a whole-gallery → 0b specific portfolio piece → 0c portfolio menu → 0d catalogue+prices → 1 photo request → 1b out-of-scope/delay/complaint → 2 service-specific pricing → 3 full pricing overview → 3b repeated-question → 4 normal `generate_response`.
- Outbound goes out on a **1–5 min random delay** via `delayed_response` in a daemon thread; a newer inbound message **cancels the pending send** (`_pending_send_events`) so the batch re-runs with the latest context.

### Unified classifier (`bot/unified_classifier.py`)
- One DeepSeek call returns a dict consumed by all downstream handlers (OOS intent, product/service intent, booking-data extraction, photo/repeat/plan-later flags), replacing ~6 separate calls; on failure it returns `None` and callers fall back to their own classifiers. Access via `uc_as_service_inquiry`, `uc_as_oos_classification`, `uc_is_photo_request`, etc.

### Conversation storage (`bot/models.py` — `Appointment`)
- `conversation_history` is a schemaless `JSONField` of `{role, content, timestamp}` dicts plus optional `message_id`/`quoted`/`media_index` keys — transcript metadata never gets a migration.
- Helpers: `add_conversation_message` (logs a turn; back-fills WAMID/quote onto a duplicate entry), `attach_message_id` (stamps an outbound WAMID after the send returns), `record_sent_media` (one entry per image batch carrying `{wamid: description}`), `resolve_quoted_message` (maps an inbound `context.id` → stored text/image description).

### Quoted-reply ("highlighted message") feature
- WhatsApp delivers only the quoted message's **WAMID** (`context.id`), never its text, so outbound WAMIDs are stamped onto history (text in `delayed_response`, images in `send_previous_work_photos` via `record_sent_media`) and resolved locally on the way back.
- The resolved quote is a **separate** `quoted_context` value that reaches classification/LLM calls only — never the rule engine. Before STEP 2, `_generate_and_schedule_reply` re-derives the service intent **deterministically** with `_keyword_product_intent` (the customer's own product word wins, else the quoted caption — e.g. a "rain shower" quote → `shower_cubicle`), so "this one how much?" on a portfolio photo prices the quoted item, not a stale carried-over intent. Deterministic on purpose: the LLM mis-maps short captions (a "rain shower" caption came back `tub_sales`).
- Fragile: resolution coverage is **not universal** — only the two stamping send-paths resolve; messages sent via direct `send_text_message` (plumber alerts, photo intro line, `generate_photo_followup`) and pre-feature history return `None` and silently behave quote-less (signature log: `🔗 … not found in history`). And the quote only steers STEP 2 intent + `generate_response`; the availability/date classifier, FAQ, and booking path don't see it.

### Portfolio / catalogue (`bot/portfolio_catalog.py`)
- Static list of previous-work pieces (title, "from" price, description, keywords). `match_portfolio_item` returns a single item only when the message clearly references one piece, else `None` → whole-gallery send; `_describe_work_image` derives a per-image description (curated title for catalogued files, tidied filename otherwise) for the media index. Prices are the business's own "from" rates from `bot/sales_profiles/homebase.md` (source of truth — keep in sync, never invent figures); captions are title-only.

### Pricing & sales (`bot/views/plumbot/response_mixin.py`)
- `detect_service_inquiry` → priceable intent; `handle_service_inquiry(intent, message)` builds the reply from a `structured_pricing` table keyed by intent (the message is used only for language detection). `generate_pricing_overview` gives the full menu; `compose_multi_answer` answers 2+ info questions in one reply.
- Pricing is gated: don't volunteer price when unasked, don't re-send an already-sent intent, and don't price a message that's a project description / booking-capture answer.

### Booking, availability, scheduling
- `extraction_mixin` pulls fields (service, area, plan status, name, datetime) and tracks `get_next_question_to_ask`; `availability_mixin` checks business-hours slots and suggests alternatives; `booking_mixin` validates completeness and books; `reschedule_mixin` handles AI-detected reschedules; `plan_upload_mixin` runs "I'll send my plan" flows and plan-status nudges; `notification_mixin` alerts the plumber and optionally Google Calendar.

### Follow-ups & cron (`bot/management/commands/`)
- `send_followups` — 4 follow-ups over 18h (0/6/12/18h) for cold leads. Other Railway crons: `send_reminders`, `send_job_reminders`, `summarize_unconfirmed_leads`, `process_inbound_emails`. (`notify_priority_leads` — the daily plumber WhatsApp alert — was removed 2026-07-08; the priority-leads dashboard pages remain.)

### Email
- Customer/transactional email goes through the **SendGrid v3 HTTP API (port 443)** in `bot/plumber_notifications.py` (`_send_via_sendgrid`); Railway blocks all outbound SMTP, so the legacy `IPv4SMTPBackend` (`bot/email_backends.py`) is a fallback only. SendGrid click/open tracking is disabled to keep `tel:`/`wa.me` links clean. HTML lives in `bot/customer_emails.py`; subjects carry `[APT-{id}]` so IMAP replies match back to the appointment.

### Supporting classifiers / safety nets
- `faq.py` (no-API canned facts), `out_of_scope_handler.py` (OOS / delay / complaint), `repeated_question_detector.py` (re-asked questions), `semantic_rescue.py` (rescues unclassifiable messages), `service_type_classifier.py` (bathroom / kitchen / new-install), `services/lead_scoring.py` (lead prioritisation).

### Tests & the commit gate
- `tests/test_bot_responses.py` is the suite. **TEST 0** (the top block) is the API-free **deterministic regression gate** — every recurring intent/pricing/flow bug is pinned there. TEST 1+ exercise the live LLM's accuracy (fuzzy; a quality signal, not a gate).
- **Gate mode:** `PLUMBOT_GATE=1 python tests/test_bot_responses.py` runs only TEST 0, with a deterministic DeepSeek stub (`tests/deepseek_mock.py`) so it's offline and reproducible, and **exits non-zero on any failure**. `PLUMBOT_MOCK_DEEPSEEK=1` runs the full suite against the stub.
- **Dashboard view/action suite:** `python manage.py test bot` (~3s, fully offline) — `bot/test_views_actions.py` smoke-GETs every staff page in every filter/tab/pagination variant and POSTs every mutating dashboard action (detail edit, plan upload, confirm/cancel/unbook/complete, pause/resume, follow-up + reminder scheduling, quotation/template actions), asserting the DB effect with all outbound mocked. Test mode in settings.py switches to in-memory SQLite + local file storage and skips bot's migrations (three are Postgres-only RunSQL), so it never touches prod. Any new page or staff action gets a case here. Known dead feature: pause-auto-followup writes `manual_followup_paused` fields that migration 0018 removed — pinned as an `expectedFailure` until re-added properly.
- **Pre-commit hook:** `.githooks/pre-commit` runs the TEST 0 gate AND the dashboard suite, blocking the commit on failure. Enable once per clone: `git config core.hooksPath .githooks`. Bypass only in emergencies with `--no-verify`.
- When adding a TEST 0 case that calls a helper using other `ResponseMixin` methods, the fake-self in the test must expose those methods/attrs (e.g. `_should_volunteer_pricing` needs `_is_job_quote_request` → `_names_multiple_products` → `_PRODUCT_FAMILY_PATTERNS`).

### Conventions to follow
- Reuse existing infra; no new dependencies without being asked. Preserve WAMID dedup and exit-signal-first ordering.
- **Keep the quote out of the rule engine** — thread it as `quoted_context` to classification/LLM calls only; any new outbound send path must stamp its WAMID (`attach_message_id` / `record_sent_media`) or quotes to it break silently.
- Prefer deterministic resolvers over LLM round-trips for short/fuzzy strings (see the quote intent fix); reserve the LLM for genuinely ambiguous language.
- **The customer's own words override any gate or holding state.** Every gate that auto-replies (price) or parks the lead in a flow must let a real signal in the *current* message — a named product, an explicit price ask, an elliptical "this one?" on a quoted photo, an exit/delay signal — win over a carried-over LLM intent or a pending flow state. This one bug recurred four times in four code paths (tub misclassification; area reply → unprompted price ×2; price question swallowed by the delay-timeframe wait). The shared deterministic resolvers are `_correct_service_intent`, `_is_unprompted_carryover_pricing` (+ `ResponseMixin._is_carryover_pricing`), `_is_quoted_item_reference`, `_delay_breakout_inquiry`, `_should_volunteer_pricing`, `_is_purchase_commitment`, `_is_job_quote_request` / `_names_multiple_products` (+ `_build_combined_price_reply`, `_build_job_quote_reply`), and `wants_whatsapp_delivery`. **Any change to intent classification, the pricing gates, or flow routing MUST add/extend an API-free case in `tests/test_bot_responses.py` TEST 0** — now enforced by the pre-commit gate (see "Tests & the commit gate").
- New per-turn metadata = optional JSON keys, never new columns; new handler params stay optional with `None` defaults so existing callers keep working untouched.
- **No emojis in customer-facing copy** (logs/dashboards fine). Support English + Shona.
- **Windows-local gotcha:** handlers `print()` emoji; set `PYTHONIOENCODING=utf-8` or local shell/test runs raise `UnicodeEncodeError` (harmless on Railway's UTF-8 stdout).
- At the end of every edit, provide a suitable `git commit -m` message.
