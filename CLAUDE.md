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
- `send_followups` — **at least 4 follow-ups per lead (`FOLLOWUP_MIN_COUNT`), spaced as FRACTIONS of that lead's own WhatsApp free-form window** (24h standard / 72h CTWA), not fixed hour steps: `TIER_WINDOW_FRACTIONS` places each touch at an absolute offset from the window opening (the lead's last message), minus `FOLLOWUP_WINDOW_MARGIN_HOURS`, with `FOLLOWUP_MIN_GAP_HOURS` between sends. Absolute offsets mean a touch deferred by the nightly contact window cannot push the later ones past the close — the old cumulative cadence (COLD 4+6+6+6 = 22h + jitter) overshot the 24h window and silently retired leads on three. **A CTWA lead (tapped a Facebook/Instagram click-to-WhatsApp ad) opens a 72h window instead of 24h, so it gets SIX touches** on `CTWA_FOLLOWUP_OFFSETS` (4/8/20h day one, 32/48h day two, 66h day three — the old 4/8/24/48 stopped at 48h and wasted the last day). `has_extended_window` gates that: an ad lead who replied late, with only a standard window left (< `CTWA_EXTENDED_MIN_HOURS` = 36h), falls back to the four-touch tier schedule rather than cramming six into a day. Offsets are scaled down proportionally when the remaining window is shorter than 72h. `max_followups_for` is the single source of the per-lead count (cron retirement, the UI chip, the dashboard due-list, the LLM prompt). **The schedule is planned against SENDABLE hours** — the messaging window intersected with `CONTACT_WINDOWS` (08:21–20:53): `_sendable_hours` shrinks the span when the window's tail falls in the quiet hours, `_scheduled_due_at` rolls a due moment forward out of the quiet hours and, when that roll would land after the window has shut, pulls it BACK to `_last_sendable_moment` minus `LAST_CALL_GRACE_MINUTES` (`_window_moment_before` is the mirror of `_next_window_open`). Without that pull-back a touch due at 02:00 on a window closing at 06:00 waited for 08:21, by which time free-form sending was dead — so it only reached the lead when they messaged again, arriving as a stale nudge on top of their live message. On a last call the spacing relaxes to `LAST_CALL_MIN_GAP_HOURS`. Two guards keep a touch off a live exchange: `FOLLOWUP_LIVE_CONVERSATION_MINUTES` (the lead just wrote — the bot's own reply is the touch; this also replaces the old 2-minute eligibility prefilter) and `FOLLOWUP_QUIET_AFTER_OUTBOUND_HOURS` (we just wrote). The delay-ghost and parked nudge loops share the last-call rule via `_is_last_call`. Delay-flow and parked nudges use the same rule (`_DELAY_NUDGE_FRACTIONS`, `_PARKED_NUDGE_FRACTIONS`) and also do 4 — parked nudges moved from 3/7 DAYS (always outside the window, so every one bounced 131047) into the back half of the window. A closed window still blocks everything: we never pay for templates. Other Railway crons: `send_reminders`, `send_job_reminders`, `summarize_unconfirmed_leads`, `process_inbound_emails`, `send_scheduled_followups`. **Every service in the Railway project shares the repo's `railway.json`, whose `deploy.startCommand` overrides each service's own** — so all of them boot `start.sh`, which runs `manage.py <job>` when `PLUMBOT_CRON` is set (comma-separated for a service carrying several jobs) and gunicorn when it isn't. Adding a cron = new service + `cronSchedule` + a `PLUMBOT_CRON` value, never a start command in the dashboard (it would be overridden). The `_cronsReference` block in `railway.json` documents the current mapping; Railway does not read it. (`notify_priority_leads` — the daily plumber WhatsApp alert — was removed 2026-07-08; the priority-leads dashboard pages remain.)

### Email
- **Inbound: the bot answers email too** (`process_inbound_emails`, 5-min cron), but **EMAIL IS REPLY-ONLY: it answers only people already in the system with a WhatsApp record, and no email ever creates a lead** (2026-08-24 rule; cold-email intake and its `INBOUND_LEAD_ADDRESSES` address->tenant routing were removed). A reply on a thread we started is matched by the `<apt-{id}.…>` Message-ID (or a legacy `[APT-{id}]` subject tag); an email with no thread reference is matched to a lead by `customer_email` (`_known_lead_for_sender`, most recently updated wins, subject folded into the customer's turn since off-thread the subject often IS the request). Either way the matched lead must pass `_is_whatsapp_lead` — a real phone number, not a synthetic `email_…` (legacy cold-email row) or `quotation_only_…` key — so the check is about the LEAD, not about how the mail matched. Everything else (receipts, support tickets, strangers writing in cold) belongs to nobody in the CRM and is left UNREAD for a human. No routing is needed for multi-tenancy: the matched lead carries its own tenant. **Two gates sit in front of the rule** (the polled inbox is the operator's personal Gmail, so the bot once opened leads on Stripe receipts and replied to them): (1) `_is_our_own_mail` drops our own traffic BEFORE the thread tag is trusted — the polled address, `DEFAULT_FROM_EMAIL`, every tenant `customer_from_email`/`email_sender`, `PLATFORM_NOTIFICATION_EMAIL`, `PLUMBER_NOTIFICATION_EMAILS`, the owner accounts, and anything on `PLATFORM_EMAIL_DOMAIN`; the Bcc copy of our own customer email carries the same `[APT-{id}]` reference the customer's reply does, so an APT match alone is not proof a customer wrote. (2) `_is_automated` drops machine mail (Auto-Submitted / List-Id / X-Autoreply headers, no-reply & mailer-daemon & receipt/billing/stripe addresses, bounce/out-of-office/receipt subjects) — without that guard the bot email-loops. Mail the bot declines is left **UNREAD** and its body is never downloaded: fetches use `BODY.PEEK` (`_fetch_unseen_headers` → gates → `_fetch_message`), because a plain `RFC822` fetch silently marks the operator's own mail read. Any new mailbox-touching code must keep the peek-then-gate order. A `--dry-run` neither replies nor marks anything seen.
- Email-only leads have no phone number, so they are excluded from proactive WhatsApp: `send_followups` skips `email_`/`quotation_only_` prefixes and `send_reminders._send_wa` refuses any non-numeric recipient. Any new synthetic-key lead type must do the same.
- **Two sending identities per tenant** (`bot/plumber_notifications.py`): `tenant_platform_from_email` → `<tenant-slug>@notifications.homexmedia.com` (`PLATFORM_EMAIL_DOMAIN`) is used for **internal** alerts — the mail to the operator and to the tenant's own inbox; `tenant_customer_from_email` → the tenant's own domain address (`TenantProfile.customer_from_email`, set on the Profile page) is used for **everything the tenant's clients receive**, falling back to that platform sender when unset, and to `DEFAULT_FROM_EMAIL` only for tenant-less platform mail (password resets). Resolution happens once at the `send_email_to_recipients` choke point and is threaded to all three transports as `from_email`/`reply_to`; only `send_plumber_notification_email` passes an explicit `from_email`. Reply-To follows From on any tenant-scoped send, so a customer's reply reaches the tenant, never the platform inbox. **One shared authenticated domain with a per-tenant local part — deliberately not a subdomain per tenant: every distinct domain needs its own SPF/DKIM records and consumes one of Brevo's authenticated-domain slots (the account is on the free tier). DNS: `notifications.homexmedia.com`, and any tenant domain, must be SPF/DKIM-authenticated in Brevo before it delivers.** Note `email_sender` is a recipient inbox, `customer_from_email` a sending identity — never conflate them.
- Customer/transactional email goes through the **SendGrid v3 HTTP API (port 443)** in `bot/plumber_notifications.py` (`_send_via_sendgrid`); Railway blocks all outbound SMTP, so the legacy `IPv4SMTPBackend` (`bot/email_backends.py`) is a fallback only. SendGrid click/open tracking is disabled to keep `tel:`/`wa.me` links clean. HTML lives in `bot/customer_emails.py`; subjects carry `[APT-{id}]` so IMAP replies match back to the appointment.

### Quote screens (mobile-first)
- Nine templates make up the quote workflow: `create_quotation` (from a lead), `standalone_quotation` (`quotations/new/`), `edit_quotation`, `view_quotation`, `quotations_list`, and the four template-builder pages. **All of them extend `bot/layouts/base.html` and share one responsive layer, `bot/includes/quote_responsive_css.html` (`pbq-*`)** — include it in `{% block extra_css %}` on any new quote screen rather than re-inventing card/table/modal CSS.
- The mobile contract, pinned by `QuoteMobileLayoutTests` in `bot/test_views_actions.py`: one document per page (view/edit used to emit a whole second `<!DOCTYPE html>` inside the content block, so their `body{background}` painted over the shell); no declared width or `min-width` wider than a phone; the Font Awesome icon set only (`bi bi-*` renders blank — Bootstrap Icons is never loaded); and the shared layer present on every page.
- Wide tables never scroll sideways: read-only ones take `.pbq-table--stack` and editable ones `.pbq-table--edit`, and **every `<td>` must carry a `data-label`** — below 768px the header row is hidden and each cell prints its own label as the row becomes a card. `.pbq-table--edit` uses `!important` on `display`/`width` deliberately: it sits over legacy page CSS that pins column widths with `nth-child`, which would otherwise outrank it. Key/value panels use `.pbq-kv` (a list, identical at 320px and 1440px) instead of a one-column table.
- The pinned 4-up bar (`.pbq-actionbar`) is **`position: sticky`, offset by `--bottom-nav-h`** so it clears the mobile bottom nav. It is not `fixed` on purpose — `.pb-content__inner` runs `pb-fadein` with `animation-fill-mode: both`, and a retained `transform` makes it the containing block for fixed descendants. That keyframe now ends on `transform: none` for the same reason; **don't put a `translateY(0)` back**, or every in-content modal and pinned bar silently starts scrolling with the page.
- `quotations_list` is `paginate_by = 25` and now renders page controls; without them everything past the first 25 quotes was unreachable.

### Dashboard permissions & the tenant's own notification inbox
- **Owner-only:** **deleting a lead/conversation** (`delete_appointment`) — irreversible cascade, so superuser is deliberately not enough. `@owner_required` / `is_platform_owner` (`bot/decorators.py`) match the login against `settings.PLATFORM_OWNER_ACCOUNTS` (env `PLATFORM_OWNER_ACCOUNTS`, default `adminJ,jones86xi@gmail.com`; usernames or emails, case-insensitive). An **empty** list degrades to "any superuser" on purpose so a mis-set env var can't lock the owner out. Templates gate on `is_platform_owner`, injected by the `plumbot_shell` context processor.
- **Superuser-only:** the platform console and the Settings pages (`settings` / `calendar_settings` / `ai_settings` — platform config, not tenant controls), via `@superuser_required` plus `{% if user.is_superuser %}` in `main_nav.html` / `mobile_nav.html`. Always gate the view, never only the template.
- Each tenant picks the inbox its own internal alerts go to on the **Profile page** (`notification_email` → `TenantProfile.email_sender`). `get_plumber_notification_emails(tenant)` returns `PLATFORM_NOTIFICATION_EMAIL` (jones86xi@gmail.com — constant for every tenant) plus that address. **The platform address is never shown to tenants**: `split_notification_recipients` puts the tenant's own addresses in To and the operator in **Bcc** (threaded through all three transports), and the Profile page lists only the visible half — hiding it in the UI alone would be pointless while it sat on the To line of a mail the tenant reads. The one exception is a tenant with no chosen address: there is nobody else for To, so it stays visible rather than sending with no recipient; with no choice made, only `tenant=None` and the homebase seed fall back to `settings.PLUMBER_NOTIFICATION_EMAILS`, so a new tenant never mails Homebase's inbox. Any new plumber-alert send path must pass the tenant.

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
- **No Homebase value may reach another tenant's customer.** Every figure, number, name and place in customer-facing copy or an LLM prompt resolves through the lead's own tenant: prices via `TenantConfig` (`catalogue_price_lines`, `price_components`, `freestanding_tub`, `cheapest_labour_rate`), the plumber's line via `Appointment.plumber_contact()` (webhook alerts go through `_plumber_wa_number`; `send_reminders` groups briefings per tenant via `_tenant_plumber` and gives customers `_lead_plumber_contact`), the name via `business_name_for()` / `plumber_display_name()`, the place via `location_short()` / `location_city`. **Absent means omit, never borrow** — a tenant with no tub price gets the free-visit deflection, one with no plumber number gets the email alert only. The `PLUMBER_PHONE_NUMBER` / `PLUMBER_NAME` env vars are homebase's legacy fallback and must not be used for a lead that has a tenant. Fixed 2026-08-21: catalogue price sheet, tub pricing, both plumber-alert paths, the unified + service-type classifier prompts, and the "based in Harare" / "as little as US$20" / "the plumber's name is Takudzwa" copy were all still hardcoded.
- **Hours are the tenant's own week, plus an optional 24/7 emergency tick.** `business_hours['emergency_24h']` (a key on the same JSON — no column) is set from the Profile editor and the intake wizard; `TenantConfig.emergency_24h()` / `emergency_sentence()` / `emergency_tag()` are the only readers, and every hours-copy site (`_working_hours_line`, `_hours_clause`, `_open_hours_clause`, `_quick_hours`, `_grounding_facts`, `_hours_clock`, `_emergency_fact`, the hours FAQ fact, the email prompt) goes through them. Out-of-hours refusals end with the emergency offer instead of dead-ending the lead (`_emergency_offer` in both `models.Appointment` and `availability_mixin`). It does NOT widen the booking window — regular slots stay inside the tenant's day. Absent means omit: never promise 24/7 for a tenant without the tick.
- Prefer deterministic resolvers over LLM round-trips for short/fuzzy strings (see the quote intent fix); reserve the LLM for genuinely ambiguous language.
- **The customer's own words override any gate or holding state.** Every gate that auto-replies (price) or parks the lead in a flow must let a real signal in the *current* message — a named product, an explicit price ask, an elliptical "this one?" on a quoted photo, an exit/delay signal — win over a carried-over LLM intent or a pending flow state. This one bug recurred four times in four code paths (tub misclassification; area reply → unprompted price ×2; price question swallowed by the delay-timeframe wait). The shared deterministic resolvers are `_correct_service_intent`, `_is_unprompted_carryover_pricing` (+ `ResponseMixin._is_carryover_pricing`), `_is_quoted_item_reference`, `_delay_breakout_inquiry`, `_should_volunteer_pricing`, `_is_purchase_commitment`, `_is_job_quote_request` / `_names_multiple_products` (+ `_build_combined_price_reply`, `_build_job_quote_reply`), and `wants_whatsapp_delivery`. **Any change to intent classification, the pricing gates, or flow routing MUST add/extend an API-free case in `tests/test_bot_responses.py` TEST 0** — now enforced by the pre-commit gate (see "Tests & the commit gate").
- New per-turn metadata = optional JSON keys, never new columns; new handler params stay optional with `None` defaults so existing callers keep working untouched.
- **No emojis in customer-facing copy** (logs/dashboards fine). Support English + Shona.
- **Windows-local gotcha:** handlers `print()` emoji; set `PYTHONIOENCODING=utf-8` or local shell/test runs raise `UnicodeEncodeError` (harmless on Railway's UTF-8 stdout).
- At the end of every edit, provide a suitable `git commit -m` message.
