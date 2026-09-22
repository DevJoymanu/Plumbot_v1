# Plumbot – Claude Code Instructions

## Project Overview
Plumbot is a WhatsApp-based appointment scheduling and sales chatbot for Homebase Plumbers in Harare, Zimbabwe. It is built with Django, deployed on Railway, uses Twilio for WhatsApp messaging, and DeepSeek API for AI-powered intent classification and response generation.

## Core Files
- `whatsapp_webhook.py` / `views.py` — main conversation flow logic
- `send_followups.py` — Railway cron job for follow-up scheduling
- DeepSeek API integration — intent classification and response generation

## Coding Rules
- **Never change an established owner rule without asking the owner first (owner, 2026-09-22).** This covers the non-negotiables below, the rules in `docs/current-state/`, and any behaviour the owner set in conversation. If a new request seems to contradict a rule already in place, say which rule it contradicts and ask, even when the request looks explicit. The owner may be refining the rule rather than replacing it; the price-ask case was one ("a price question IS the hesitation signal", so offering online there is not a contradiction of "lead with the visit"). Only change the rule once the owner has confirmed. Then record the new rule and its reason in the doc for that area, in the same commit.
- Never introduce new dependencies unless explicitly asked
- Reuse existing infrastructure and patterns already in the codebase
- Always preserve WAMID deduplication logic — never remove it
- Exit-signal detection must always run before any flow-stage logic
- Never re-pitch the site visit to a customer who has already committed
- **Speak as WE, never "the plumber" (owner rule, stated more than once — last on 2026-09-18).** Customer copy says "once we see the space", "we'll come through", "we'll call you", never "the plumber sees the space", "our plumber will come" or "your plumber is on the way". Write new copy that way at source. The net under it is `bot.utils.speak_as_we`, applied at every outbound choke point (`finalise_outbound`, `delayed_response`, `Appointment.add_conversation_message` for assistant turns, `dequalify_free_visit` for cron copy) and pinned by the `we voice:` cases in TEST 0 — **do not remove it from any of them, and any new send path must go through one.** Customer emails are outside that net, so their copy must say "we" at source (TEST 0 scans `customer_emails` and `plan_upload_mixin`). Only two exceptions: a lead who asks who is coming may be told the name, and internal copy to the plumber (alerts, dashboards) is not customer copy.
- **Every new function, new piece of functionality, and every edited block of code carries a comment saying WHAT it does, WHY it is there, and HOW it works.** This is not optional tidying: almost everything in the "Current State" notes below is a rule someone had to rediscover from production, and a comment at the code is the only copy of it that a future session is guaranteed to read. Applies to NEW code and to code you CHANGE, in Python, in template `{# #}` blocks (which cannot span lines - `TemplateCommentTests` catches that), and in the editors' JS.
  - **New function / method / class:** a docstring (or a comment above it where the file's style has no docstrings) covering the three questions - what it answers, why it exists rather than the obvious alternative, and how it gets there: inputs, the order things run in, and what it returns when it cannot answer.
  - **New functionality inside an existing function:** a short comment above the block, naming the behaviour rather than the syntax. "A bare day answer still gets the time question" beats "loop over slots".
  - **An edited block:** say what changed and what it was before, whenever the old shape is the thing a reader would otherwise put back. A bug fix carries its failing case in one line ("a size-less list line took a sized quote line and priced the wrong variant"), because that sentence is what stops the fix being reverted.
  - **WHY outranks WHAT.** The code already says what it does. Write down the reason it is this way: the owner rule behind it, the production failure it prevents, the ordering it depends on, the thing it deliberately does NOT do. A deterministic resolver standing in for an LLM call, a gate held back on purpose, an English-only pattern, a choke point every send must pass through - each needs its reason beside it or the next edit removes it.
  - **Point at the net.** Where a rule is pinned by a test, name it in the comment (`pinned by the "budget ladder" cases in TEST 0`, `PostVisitBacklogGuardTests`), so an edit that breaks it knows where it will be caught.
  - Comments describe the code as it stands, never the conversation that produced it: no "as requested", no "new", no change-log dates on a line that will be edited again. Keep them true - a stale comment is worse than none, so when the code changes, the comment above it changes in the same edit.

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

**The detail lives in [docs/current-state/](docs/current-state/README.md), one file per area, and you must read the file for the code you are about to change BEFORE changing it.** This root file used to carry all of it (26,000 words, loaded on every turn), which is exactly how a rule for the file you were editing got lost in paragraph 40 of a section about something else. It now holds only what applies everywhere. A nested `CLAUDE.md` in each code directory loads when you touch a file there and names the doc(s) to read:

| Before editing... | Read |
|---|---|
| `bot/whatsapp_webhook.py`, `unified_classifier.py`, `controller*.py`, `response_check.py`, anything that SENDS | [pipeline.md](docs/current-state/pipeline.md) + [conversation-rules.md](docs/current-state/conversation-rules.md) |
| `bot/views/plumbot/*` (response, extraction, availability, booking, reschedule mixins) | [sales-and-pricing.md](docs/current-state/sales-and-pricing.md), [conversation-rules.md](docs/current-state/conversation-rules.md), [booking-and-scheduling.md](docs/current-state/booking-and-scheduling.md), and load the `plumbot-sales-flow` skill |
| `bot/management/commands/*` (crons) | [followups-and-cron.md](docs/current-state/followups-and-cron.md), [post-visit-and-plan-path.md](docs/current-state/post-visit-and-plan-path.md), [email.md](docs/current-state/email.md) |
| `bot/post_visit.py`, `plan_quote.py`, `visit_proposal.py`, `plan_detection.py` | [post-visit-and-plan-path.md](docs/current-state/post-visit-and-plan-path.md) |
| Quote views, quote templates, `quote_pdf.py` | [quotes.md](docs/current-state/quotes.md) |
| Dashboard views and templates | [dashboard-ui.md](docs/current-state/dashboard-ui.md) (and load the `plumbot-ui-design` skill) |
| Anything touching email | [email.md](docs/current-state/email.md) |
| Anything with a figure, name, number, place, logo or permission in it | [tenancy-and-permissions.md](docs/current-state/tenancy-and-permissions.md) |

When you learn something that belongs in those notes, write it into the doc for that area, in the same commit as the code, never back into this file.

### Non-negotiables (each is a production failure that already happened; detail behind the link)
- **No Homebase value may reach another tenant's customer.** Every figure, name, number and place resolves through the lead's own tenant; absent means omit, never borrow. [tenancy](docs/current-state/tenancy-and-permissions.md)
- **The customer's own words override any gate or holding state** (a carried-over LLM intent or pending flow state never outranks what they just said). [conversation-rules](docs/current-state/conversation-rules.md)
- **Every outbound reply goes through the chain**: `finalise_outbound`, or `_finalised_for_send` for a path that writes to the wire itself, and the transcript records what was SENT via `replace_draft_assistant_turns`. A new send path also stamps its WAMID. [pipeline](docs/current-state/pipeline.md)
- **We never tell a customer they have an appointment the row does not have** (`strip_unbacked_confirmation`). [pipeline](docs/current-state/pipeline.md)
- **The free visit, and a charged visit's fee, are said ONCE.** [conversation-rules](docs/current-state/conversation-rules.md)
- **No emojis and no dash punctuation in customer copy**; speak as WE (Coding Rules above). [conversation-rules](docs/current-state/conversation-rules.md)
- **The owner's own copy is not "improved" unasked**: the follow-up scripts, the materials-list reply, the price close. [followups-and-cron](docs/current-state/followups-and-cron.md), [sales-and-pricing](docs/current-state/sales-and-pricing.md)
- **Scope at the query, gate the view (never only the template), and mutate only on POST.** [tenancy](docs/current-state/tenancy-and-permissions.md), [quotes](docs/current-state/quotes.md)
- **Synthetic-key leads (`email_…`, `quotation_only_…`) never get proactive WhatsApp.** [email](docs/current-state/email.md)

### Tests & the commit gate
Three layers, and they catch different things. **A bug fix is not done until one of them would have caught it.**

- **TEST 0: resolvers.** `tests/test_bot_responses.py` (the top block) is the API-free **deterministic regression gate**: every recurring intent/pricing/flow bug is pinned there. TEST 1+ exercise the live LLM's accuracy (fuzzy; a quality signal, not a gate). **Gate mode:** `PLUMBOT_GATE=1 python tests/test_bot_responses.py` runs only TEST 0, with a deterministic DeepSeek stub (`tests/deepseek_mock.py`) so it's offline and reproducible, and **exits non-zero on any failure**. `PLUMBOT_MOCK_DEEPSEEK=1` runs the full suite against the stub. When adding a TEST 0 case that calls a helper using other `ResponseMixin` methods, the fake-self in the test must expose those methods/attrs (e.g. `_should_volunteer_pricing` needs `_is_job_quote_request` → `_names_multiple_products` → `_PRODUCT_FAMILY_PATTERNS`).
- **Scenarios: whole conversations.** TEST 0 checks each resolver in isolation. It cannot see the right resolver running while the router picks the wrong branch, or the right branch sending the wrong one of five copies of a sentence, and that is most of what actually ships broken. `bot/test_scenarios.py` replays every `scenarios/*.txt` through the real inbound pipeline with the DeepSeek stub (~5s) and fails when a check that passes in `scenarios/offline_baseline.json` stops passing. Checks only a live model can pass are recorded as failing there, so the baseline only ever gets stricter; `PLUMBOT_SCENARIO_LIVE=1 python manage.py test bot.test_scenarios` replays all of them against real DeepSeek and requires every one. **Every live-API test (that, `run_scenarios`, `manage.py chat`, the Scenario Lab, TEST 1+ without `PLUMBOT_GATE=1`) is paid and runs ONLY after asking the owner, every time; there is deliberately no scheduled live run.** The offline gates make no API calls and need no permission. **Every production conversation bug gets a scenario file**: see `scenarios/CLAUDE.md` for the format. After a DELIBERATE behaviour change: `PLUMBOT_SCENARIO_BASELINE=write python manage.py test bot.test_scenarios`, then read the diff; a check flipped from true to false is a regression being written down, not fixed. Keep `tests/deepseek_mock.py` faithful: its unified-turn answer was dead offline for months, because a generic yes/no branch matched a sentence in the unified prompt, and nothing noticed until the replay needed it.
- **Dashboard view/action suite:** `python manage.py test bot` (fully offline; runs the scenario replay above too). `bot/test_views_actions.py` smoke-GETs every staff page in every filter/tab/pagination variant and POSTs every mutating dashboard action (detail edit, plan upload, confirm/cancel/unbook/complete, pause/resume, follow-up + reminder scheduling, quotation/template actions), asserting the DB effect with all outbound mocked. Test mode in settings.py switches to a throwaway SQLite FILE in the temp dir (one per run, with a busy timeout, because reply threads write concurrently and in-memory SQLite failed those writes with "database table is locked"; `Plumbing_CRM/test_runner.py` sweeps leftovers) + local file storage and skips bot's migrations (three are Postgres-only RunSQL), so it never touches prod. Any new page or staff action gets a case here. Known dead feature: pause-auto-followup writes `manual_followup_paused` fields that migration 0018 removed — pinned as an `expectedFailure` until re-added properly.
- **Where they run:** `.githooks/pre-commit` runs TEST 0 and `manage.py test bot` (so the scenario replay too), blocking the commit on failure; enable once per clone with `git config core.hooksPath .githooks`, and bypass only in emergencies with `--no-verify`. `.github/workflows/gate.yml` runs the same two on every push and PR, which is the backstop a local `--no-verify` cannot skip.

### Conventions to follow
- Reuse existing infra; no new dependencies without being asked. Preserve WAMID dedup and exit-signal-first ordering.
- **Keep the quote out of the rule engine** — thread it as `quoted_context` to classification/LLM calls only; any new outbound send path must stamp its WAMID (`attach_message_id` / `record_sent_media`) or quotes to it break silently.
- Prefer deterministic resolvers over LLM round-trips for short/fuzzy strings (see the quote intent fix); reserve the LLM for genuinely ambiguous language.
- **Customer-facing copy is written in ONE place and called from everywhere** (`bot/copy_catalog.py`). A sentence that exists in two places drifts: the notes kept recording it ("five OTHER copies of that sentence remain", two copied `all_day_phrases` lists "that had already drifted apart"). Before writing a sentence a customer will read, look for it in the catalog; if it is not there, add it there and call it. `bot/test_copy_catalog.py` fails when a catalogued sentence is written out anywhere else.
- **A model-written customer sentence gets a fence and a fallback** (`bot/copy_fence.py`). Code resolves the facts and hands them to the model as fixed strings; `fence_holds` rejects a composition that invents a figure, a day, a clock time or a promise word, asks other than one question or grows too long; a rejected or failed composition sends the deterministic sentence instead, which must be correct on its own. `bot/availability_ask.py` was the first user and the follow-up rewrite is the second.
- New per-turn metadata = optional JSON keys, never new columns; new handler params stay optional with `None` defaults so existing callers keep working untouched.
- **Windows-local gotcha:** handlers `print()` emoji; set `PYTHONIOENCODING=utf-8` or local shell/test runs raise `UnicodeEncodeError` (harmless on Railway's UTF-8 stdout).
- At the end of every edit, provide a suitable `git commit -m` message.
