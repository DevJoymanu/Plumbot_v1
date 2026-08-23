# Vision Plan — teaching Plumbot to see

Status (2026-08-22): **Phase 0 and Phase 1 shipped, Phase 2 partly.** The photo
now feeds `_correct_service_intent` as a gap-filler. Still open: Phase 3
(plumber-alert enrichment) and Phase 4 (the humanness pass). Not yet run live —
`manage.py run_scenarios` has not been executed against a real photo.

Couples two workstreams that share the same prompts and the same test surface:
inbound **image understanding**, and the **humanness** pass on generated copy.
They ship in that order because vision gives the tone work something worth
saying.

---

## 1. Where we are

`handle_media_message()` (`bot/whatsapp_webhook.py:3285`) currently:

- downloads bytes via `get_client_for_tenant(tenant).download_media(media_id)`
- saves to storage, appends `[FILE UPLOADED] ...` to `internal_notes`
- advances `plan_status` only when `pending_upload` (correctly guarded)
- logs `add_conversation_message("user", "[Sent image]")` — **content-free**
- fires `_schedule_media_ack()` after an 8s debounce (`MEDIA_DEBOUNCE_SECONDS`)

Three defects fall out of that:

| # | Defect | Cost |
|---|---|---|
| A | **Captions are dropped.** No `media_data.get('caption')` exists anywhere in the webhook. A photo plus "how much for this?" loses the question entirely. | Direct lost bookings — the lead asked and got a non-answer |
| B | **History carries no image content.** `[Sent image]` is all the unified classifier, pricing gates and `generate_response` ever see. | Every photo turn restarts qualification from zero |
| C | **Any image sets `has_plan=True`** (the `has_plan__isnull=True` update fires regardless of what the picture is). | A leak photo marks the lead as having architectural plans; downstream flow mis-routes |

Note the asymmetry worth closing: **outbound** images already carry text
descriptions in history via `record_sent_media` (`{wamid: description}`), which
is what makes quoted-reply resolution work. Inbound images have no equivalent.
Vision closes that loop.

---

## 2. Architecture decision

**One vision call converts image to text. Everything downstream stays text-only.**

Do *not* make the 27 existing `deepseek_call` sites multimodal. A single
describe-step at ingest writes a description into `conversation_history`, and the
entire existing pipeline — unified classifier, `_correct_service_intent`, pricing
gates, `generate_response` — runs unchanged on text it already knows how to
handle.

This mirrors `_describe_work_image()`, which already does exactly this for
outbound catalogue images. Same shape, opposite direction.

Consequences:

- No new dependency. Same OpenAI SDK, same `deepseek_call` wrapper.
- Bytes are already in hand in `handle_media_message` — send **base64**, never a
  public URL. No signed-URL plumbing, no bucket exposure.
- The description is an **optional JSON key** on the history entry, per the
  no-new-columns convention.

---

## Phase 0 — Unify the media path through the dispatcher (no vision, ship first)

The highest-ROI change in this document, and it involves no model at all.

**Root cause:** `_schedule_media_ack` (`whatsapp_webhook.py:329`) is a second,
parallel reply path with hardcoded copy and **zero state awareness**. Every
symptom below is a consequence of it bypassing the main dispatcher — the same
root cause as the quoted-reply coverage gaps already documented in CLAUDE.md.

Four bugs it causes today:

1. **Captions are discarded** (defect A) — no `media_data.get('caption')` exists.
2. **It re-asks a question we already answered.** It sends "Could you describe
   what you'd like done" even when `project_description` is already populated,
   because it never consults `get_next_question_to_ask()`. It also never reaches
   the semantic duplicate detector, which lives on the text path.
3. **It poisons the following turn.** The ack is written into
   `conversation_history` as an assistant turn, so `_last_assistant_was_tiedown()`
   and the duplicate detector read corrupted state on the *next* message.
4. **It sends via direct `send_text_message`**, so no WAMID is stamped and a
   customer quoting the ack silently resolves to `None`.

### The fix

Delete the hardcoded copy. Log the caption (when present) as the user's turn,
then route media through `_generate_and_schedule_reply`. Keep the 8s debounce —
it does real work (three photos, one reply). Only the canned string goes.

The reply then becomes acknowledgement + `get_next_question_to_ask()`, which
already encodes the right precedence:

| State | Correct reply |
|---|---|
| `project_description` empty | Today's question — correct in this case only |
| Description set, no `customer_area` | Ack, then "Whereabouts are you based?" |
| Description + area, no datetime | Ack, then the day/time question |
| `complete` / already confirmed | **Pure acknowledgement, no question.** Never re-pitch someone who already committed |

Two-message split applies throughout: ack, beat, question — never one block.

### Acknowledgement copy rules

- **Never name the plumber.** Refer to the visit impersonally — "that'll be handy
  when we come round to look at the space", never "I'll make sure <name> has it".
  The name adds nothing to an ack, and a hardcoded one is a Homebase value that
  would leak into another tenant's customer copy. Absent means omit, never borrow.
- Casual visit framing per the sales rulebook — "a quick look at the space", never
  a formal assessment pitch.
- No emojis. Mirror the customer's language.

### Also fix here

- **Defect C:** only set `has_plan=True` when `plan_status == 'pending_upload'` —
  i.e. when we actually asked for a plan. Today any image sets it.
- **A PDF is the one upload that genuinely IS a plan.** Acknowledge it as such
  rather than asking them to describe what they just drew — but only ask a
  follow-up question if `get_next_question_to_ask()` says one is outstanding.
- **Gate the mime type** so PDFs never reach Phase 1's vision call (DeepSeek
  accepts JPEG/PNG/GIF/WebP only — a PDF would 400).

**Rule this obeys:** *the customer's own words override any gate.* A caption is
the customer's words; it must beat the canned ack exactly as a named product
beats a carried-over LLM intent.

Ships independently. No cost, no model, no new dependency.

---

## Phase 1 — Describe the image

### Verified model facts (checked against api-docs.deepseek.com, 2026-08-22)

- Exact model id: **`deepseek-v4-flash-vision-exp`** — confirmed live on our
  account via `GET /v1/models`.
- Priced **identically to `deepseek-v4-flash`**: $0.22/$0.66 per 1M off-peak,
  double at peak, $0.007 cache-hit input.
- Images auto-resize to a ~800x800 equivalent; **384 tokens per image is a hard
  upper bound**, so a 5000x5000 photo costs the same as a 2000x2000 one. No need
  to downscale client-side for cost reasons.
- `"detail": "low"` forces a 512x512 downscale — cheaper and faster. Worth
  A/B-ing; a "which fixture is this" question probably does not need full res.
- Formats: **JPEG, PNG, GIF, WebP only.** Limits: 32 MiB per image (base64),
  8192 px per side.
- **Images may appear only in `user` messages.** An image in a system or
  assistant message returns 400.
- **Thinking mode: the docs are wrong.** The pricing page says vision has no
  thinking mode. Measured 2026-08-22, it emits **450-700 reasoning tokens** when
  left alone, which on a 150-token budget consumed the entire allowance and
  returned empty content (`finish_reason=length`) on 4 of 5 test photos. It
  **accepts `thinking: {"type": "disabled"}`** and then answers in ~40 tokens.
  So the existing unconditional patch in `services/clients.py` is correct as-is
  and must NOT be given a vision carve-out. Trust the measurement, not the doc.
- JSON mode (`response_format: json_object`) is **not documented as supported**
  for vision.

### Two integration blockers these create

**Blocker 1 — RESOLVED, and the opposite of what was predicted.** This document
originally said the patch must skip the vision model. That was wrong and was
tried: skipping the injection let vision default to thinking-on, and 4 of 5 test
photos came back empty. The unconditional patch in `services/clients.py` is
already correct — vision needs `thinking: disabled` exactly as much as the text
models do. No change required.

**Blocker 2 — no JSON mode means no structured extraction in the vision call.**

This is a *simplification*, not a problem. The vision call returns **one or two
plain sentences of prose**, and structured fields get extracted afterwards by the
existing text pipeline — which is exactly the architecture decision in section 2.
The image becomes text once; nothing else in the bot learns about images.

Where `kind` was going to come from a JSON field, derive it from the description
with a deterministic resolver (`_looks_like_plan_drawing`), per the
prefer-deterministic-over-LLM convention.

### Build steps

1. Add `describe_customer_image(file_bytes, mime_type, tenant)` in
   `bot/services/` — base64 data URL, `max_tokens` ~150, `temperature=0`,
   `model="deepseek-v4-flash-vision-exp"` (the `model=` param on `deepseek_call`
   already exists and is currently unused by every call site).
2. **Gate on mime type before calling.** `handle_media_message` treats
   `('image', 'document')` identically today, but a PDF plan is not a supported
   vision format. Only JPEG/PNG/GIF/WebP reach the vision call; documents keep
   today's behaviour untouched.
3. Fold the returned description into the logged user turn, and keep the raw
   description as an optional history key.
4. **Fail open.** On any error, fall back to today's exact behaviour
   (`[Sent image]` plus generic ack). Vision is additive; it must never be able
   to break the media path. `deepseek_call` already retries then raises — catch
   it.

---

## Phase 2 — Wire the description into the flow

- `items[]` feeds `_keyword_product_intent` / `_correct_service_intent` — a photo
  of a corner tub must price as a **built-in** tub (from US$160), never
  freestanding. Deterministic resolver first, per convention.
- **Precedence, strictly:** caption text > vision `items` > carried-over intent.
  The picture is evidence; the customer's typed words are testimony. Testimony
  wins.
- Pricing stays gated. A photo alone is a **scope statement, not a price ask** —
  `_should_volunteer_pricing` still decides. Sending a picture of a bath must not
  trigger an unprompted price.
- `kind == 'plan_drawing'` is the only path that advances plan state.

## Phase 3 — Enrich the plumber alert

`_schedule_plumber_alert` currently sends bare URLs. Add the one-line description
per file so the plumber reads "cracked seal at base of corner bath" before
tapping a link. Cheap, already computed, high perceived value.

## Phase 4 — Humanness pass (the coupled workstream)

Applies to the generative call sites only.

1. **Few-shot examples** in the system prompts, drawn from the 261 lines of real
   before/after pairs in
   `.claude/skills/plumbot-sales-flow/references/corrected-examples.md` —
   currently read by no prompt. Sits in the cached prefix, so effectively free.
2. **Banned-phrase list**: "Certainly", "I'd be happy to", "Feel free to", "Let
   me know if", "I understand that", "Great question", "Rest assured". No
   mid-conversation greetings. No restating the question before answering.
3. **Shape constraints** over tone adjectives: under 25 words, contractions,
   answer first, no preamble.
4. **Temperature: leave alone in the first pass** so the tone delta is
   measurable. Later, raise only on non-factual generative calls (follow-up copy,
   retry paraphrases) to ~0.5. Never on the FAQ fact path — pinned at 0 because
   0.4 once answered "Is the quote free" with "No" and then "yes" two turns apart
   (`response_mixin.py:908`).

Vision feeds this directly: a bot that can say "that looks like the seal's gone
at the base" sounds human in a way no prompt tuning achieves.

---

## Cost

Negligible, and it does not change the tier decision. 384 tokens per image is a
verified hard ceiling, so at 600 images per month: **~$0.05/month** at the
off-peak input rate. Stay on Flash; vision is priced identically.

Correction to an earlier draft of this document: the `thinking: disabled` patch
does **not** cover the vision call safely "by construction". The vision model
does not support thinking mode at all, so the patch must learn to skip it — see
Blocker 1 in Phase 1.

---

## Rules this must not break

- Preserve WAMID dedup and exit-signal-first ordering.
- **Any new outbound send path stamps its WAMID.** The media ack currently uses a
  direct `send_text_message` — routing captions through the main dispatcher
  (Phase 0, step 3) *fixes* an existing gap rather than adding one.
- No emojis in customer-facing copy. English and Shona both.
- Vision descriptions are **internal metadata, not copy**. Never echo a raw
  description back at the customer.
- Absent means omit, never borrow — a tenant with no price for what is in the
  photo gets the free-visit deflection, not another tenant's figure.

## Tests (non-negotiable; the pre-commit gate enforces)

- **TEST 0**, API-free, with a stubbed describer: caption beats generic ack;
  caption beats carried-over intent; photo alone does not volunteer price;
  corner-tub photo resolves to built-in pricing; non-plan image leaves `has_plan`
  untouched; vision failure falls back to today's behaviour exactly.
- **Two guard cases specific to the model's constraints:** a PDF/document upload
  never reaches the vision call (mime gate), and the client patch never sends a
  `thinking` key when the model id contains `vision`.
- **Phase 0 state cases:** an upload with `project_description` already set does
  not re-ask for it; a confirmed booking gets an acknowledgement with no question
  at all; no acknowledgement contains a plumber name.
- **Scenario file** `scenarios/photo_with_caption_price.txt` — photo plus "how
  much to fix this?" must `expect:` a real answer and `reject:` the generic
  "describe what you'd like done" ack.
- Windows: set `PYTHONIOENCODING=utf-8` before local runs.

## Sequencing

Phase 0 alone, then measure. It is free, it fixes a live leak, and it de-risks
everything after it by proving the routing change before any model is involved.
