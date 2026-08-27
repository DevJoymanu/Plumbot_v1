# Campaign Engine Plan — Facebook/Instagram → WhatsApp

Status (2026-08-26): **Not started.** No models, no views, no migrations. This
document is the agreed shape before Phase 0.

An AI campaign generator for click-to-WhatsApp lead generation: the tenant
describes their business and offer, the system produces the strategy, angles,
hooks, ads, creative direction and image prompts — and wires the approved
result into the assistant that already answers the resulting WhatsApp
conversations.

Built on the existing stack (Django + Postgres on Railway, Meta WhatsApp Cloud
API). **One new dependency, explicitly agreed: `anthropic`** — see §1.

---

## 0. The reframe — why this is not a content generator

Read literally, the brief describes a marketing content tool with a WhatsApp
flavour. Built that way here it would be a second CRM bolted next to the first:
the brief's §12–§15 (opening message, qualification, lead quality, follow-up
sequences) describe systems **already running in this repo** that have absorbed
a year of bug-fixes.

The version worth building inverts it. This codebase already owns everything to
the RIGHT of the ad click — referral capture, the 72-hour window, qualification,
lead scoring, quotes, jobs, revenue. It has never owned anything to the LEFT.

> **Build the missing left half, and make generated assets CONFIGURE the running
> bot rather than describe a parallel one.**

That single constraint is the difference between a text factory and a closed
loop. It also satisfies the brief's own §23 for free: every generated asset
either writes into something the bot executes, or maps to a funnel metric the
CRM can already compute. If it does neither, it does not get generated.

---

## 1. Provider split decision

**Claude (Anthropic API) for campaign generation. DeepSeek stays for the
runtime — chatbot, email, WhatsApp.**

| Surface | Provider | Why |
|---|---|---|
| Chatbot, classifiers, email replies, extraction | **DeepSeek** (unchanged) | Thousands of short, latency-sensitive calls a day. Already tuned, already gated by TEST 0. Nothing about it changes. |
| Campaign generation, §19 performance analysis | **Claude API** (`anthropic` SDK) | Long-form structured generation, low volume, high value per call. Schema-validated output, 128K output ceiling, prompt caching across a multi-step pipeline. |

### 1.1 Why not the alternatives

| Option | Verdict |
|---|---|
| DeepSeek for everything | The thinking-mode/truncation class of bug (`bot/services/clients.py`) is worst exactly where output is longest. Campaign JSON is the worst case for it. |
| Claude Agent SDK (Claude Code as a library) | Harness-only, built for open-ended filesystem/tool work. The pipeline is 13 deterministic steps with schemas — no tools needed. Heavy to deploy on Railway. |
| Managed Agents | Anthropic hosts the loop and a sandbox. Overkill: there is no sandbox work, and the orchestration is ours. |
| **Claude Messages API (chosen)** | One HTTP call per step, structured outputs, prompt caching, no infra. |

### 1.2 The boundary is physical and tested

`deepseek_call` in `bot/services/clients.py` stays exactly as it is. The Claude
client lives in its own module, used ONLY by `bot/campaigns/`.

Pin it the way this repo pins everything else — an API-free test asserting:

- nothing under `bot/campaigns/` imports the DeepSeek client
- no runtime bot module (`whatsapp_webhook`, `views/plumbot/*`,
  `management/commands/*`) imports `anthropic`

Without that test the boundary erodes silently: a shared helper gets reused and
either the chatbot starts calling Claude at runtime volume, or the generator
quietly degrades to DeepSeek and nobody notices until the copy gets worse.

### 1.3 API specifics that shape the design

- **Model: `claude-opus-5`.** Adaptive thinking (`thinking={"type":"adaptive"}`)
  and `output_config={"effort": ...}`. Routing mechanical steps to a cheaper
  model later is a quality call for the operator to make, not a default.
- **Structured outputs.** `output_config.format` / `messages.parse()` validates
  each step's response against its schema. This deletes a whole layer of
  hand-rolled JSON parsing — but only the SHAPE. Truth validation (§7) is still
  ours.
- **Prompt caching restructures the prompt.** The tenant brief + strategy is a
  stable prefix reused by all 13 steps: put it FIRST with a cache breakpoint,
  per-step instructions after. Roughly 90% off the shared portion. Ordering the
  prompt stable-first is therefore an architectural decision, not a style one.
  Verify with `usage.cache_read_input_tokens` — if it is zero across a run, a
  silent invalidator (a timestamp, an unsorted dict) is in the prefix.
- **Streaming** for the long steps; `max_tokens` generous. Truncation is not the
  standing threat it is on DeepSeek.
- **`stop_reason == "refusal"`** must be checked before reading content, and
  server-side fallbacks enabled by default.
- **Failure semantics are the OPPOSITE of the bot's.** DeepSeek classifiers here
  degrade gracefully (return `None`, fall back to keyword resolvers) because a
  customer is waiting. A campaign step must fail LOUDLY and stay retryable —
  never a silently half-generated campaign. Same allergy to swallowed
  exceptions that the reschedule phantom-method incident earned.

### 1.4 Cost

A full 13-step run with a cached brief lands **under US$1** on Opus 5, most of
it output tokens. Regenerating one step is cents. For a feature that produces a
launch-ready campaign that is negligible — and it is precisely why the runtime
chatbot, at thousands of calls a day, stays on DeepSeek.

Levers if volume grows: the Batches API runs at 50% cost asynchronously, and
generation is already a background job. `messages.count_tokens` before a run
gives a per-tenant spend cap.

### 1.5 Deployment

- `anthropic` added to `requirements.txt` (the house no-new-dependencies rule is
  explicitly waived for this one).
- `ANTHROPIC_API_KEY` as a Railway env var. Platform-level, not per-tenant — it
  does not need the Fernet treatment `TenantWhatsAppChannel.access_token` gets.

---

## 2. The attribution spine

Meta attaches a `referral` object to the first WhatsApp message from an ad
click. The webhook already reads it (`bot/whatsapp_webhook.py`, the
`message.get('referral')` path) and `Appointment.record_ctwa_referral()` already
stores:

- `ctwa_source_id` — **the Meta ad id**
- `ctwa_referral` — the full referral object, headline and body included
- `ctwa_entry_at` — start of the 72h free-form window
- `lead_source` — `facebook_ad` / `instagram_ad`, deterministically

**`ctwa_source_id` is the join key.** Put it on a `CampaignAd` row and the whole
funnel joins — ad → conversation → qualified lead → quote → paid job — with no
pixels, no UTM parameters, no new tracking infrastructure.
`backfill_ctwa_referrals` means historic leads join too.

```
Meta ad ─▶ creative+copy ─▶ CTWA click ─▶ first message ─▶ lead row ─▶
qualification ─▶ quote ─▶ job/revenue
└──── the new half ─────┘ └──────── already in the database today ────────┘
```

Cost-per-qualified-lead and ROAS **per ad** become computable the day the join
column exists, before a single line of AI copy is written. That is Phase 0.

---

## 3. Current-state audit — what already exists

Roughly 60% of the brief is a rename of something running. Mapping it honestly
is the difference between three weeks and three months.

| Brief section | Existing seam | Reality |
|---|---|---|
| §1 campaign creation form | `TenantProfile`, `TenantPriceItem`, `TenantIntake` | Business name, location, hours, services, prices, WhatsApp number all stored per tenant. The form is **pre-filled and confirm-only**, not fifteen blank fields. |
| §3 offer strategy | `bot/views/offer.py` → `TenantPriceItem(family='package', variant='facebook')` | The tenant already sets the "Facebook special" the bot leads with on a vague "how much". Campaign offers write INTO this row. |
| §12 click-to-WhatsApp capture | `record_ctwa_referral()`, `ctwa_entry_at` | Referral parsed, window opened, source attributed. |
| §13 qualification flow | `extraction_mixin`, `REQUIRED_FLOW_FIELDS` | One question at a time, four fields, duplicate-question detection, partial answers. Configure, don't regenerate. |
| §14 lead scoring | `bot/services/lead_scoring.py` | Scores 0–100 on flow completion. Has NO notion of fit — that part is genuinely new. |
| §15 follow-up sequences | `send_followups`, `CTWA_FOLLOWUP_OFFSETS` (4/8/20/32/48/66h) | Six touches for ad leads, window-fraction spacing, quiet hours, last-call pull-back. Generated copy feeds THESE slots. |
| §17 testing (WhatsApp half) | Scenario Lab, `run_scenarios`, TEST 0 | A generated opening message and flow can be replayed through the real pipeline before going live. |
| §18 metrics below the click | `Appointment`, `Quotation`, `Job` | Conversations, qualified leads, appointments, quotes, revenue — all queryable per `ctwa_source_id`. |
| §20 generated-asset storage | `bot/lead_magnet.py` | Per-tenant generated artefact, object storage, background regeneration on config change. The pattern to copy. |

---

## 4. What genuinely does not exist

| Missing | Brief | Difficulty |
|---|---|---|
| Campaign / angle / ad / asset tables | §1, §4, §5, §20 | Straightforward Django models. |
| Strategy / angle / hook / ad-copy generators | §2–§7 | Prompt + schema work. The bulk of the build. |
| Creative direction, image prompts, video scripts | §8–§11 | Pure generation, no integration risk. |
| Per-ad opening-message routing | §12 | Small: look up `source_id` at first touch. |
| Fit-based lead quality | §14 | Deterministic rules over `excluded_areas`, service match, timeline. |
| Spend / impressions / reach / CTR / CPC | §18 | **BLOCKED.** No Meta Marketing API integration exists anywhere in this repo. Needs `ads_read` + per-client ad-account access. Manual paste-in first. |
| Publishing ads to Meta | implied | Out of scope for v1. Export copy-paste blocks. |
| Performance diagnosis | §19 | Deterministic ratios; the LLM writes only the narrative. |

**The dashboard splits in half.** Everything below the click is free.
Everything above it needs Meta. Design so the CRM-side numbers stand alone and
Meta-side ones are optional overlays — otherwise the whole page is hostage to an
App Review that may take months.

---

## 5. Data model

Five new tables in a `bot/campaigns/` package (`bot/models.py` is already ~2,900
lines). They use the repo's `_tenant_fk()` helper — non-null, `PROTECT`,
homebase default — so a campaign can never become ownerless, and follow the
house rule that per-item metadata is optional JSON keys, not new columns.

| Table | Key fields | Purpose |
|---|---|---|
| `Campaign` | `tenant`, `name`, `objective`, `budget`, `duration`, `status`, `brief` (JSON), `strategy` (JSON) | §1 intake + explicit assumptions, and the §2/§3 strategy. Status: draft → generating → ready → live → paused → archived. |
| `CampaignAngle` | `campaign`, `name`, `core_idea`, `hook`, `rationale`, `creative_rec`, `cta`, `status` | §4. Approving an angle unlocks ad generation beneath it. |
| `CampaignAd` | `angle`, `headline`, `primary_text`, `cta`, `audience`, `creative_concept`, `prefilled_message`, `opening_message`, `qualification_intent`, `meta_ad_id`, `ctwa_source_id` | §5 and §12 in ONE row — the ad and the conversation it starts are the same object. |
| `CampaignAsset` | `campaign`, `ad` (nullable), `kind`, `payload` (JSON), `version`, `status` | §20's library as one polymorphic table, not six. `kind`: hook / copy_variant / image_prompt / video_script / followup / qualification_flow / test_plan. Edit, duplicate, regenerate, approve, archive are status+version moves. |
| `CampaignMetric` | `ad`, `date`, `spend`, `impressions`, `reach`, `clicks`, `conversations` | Imported Meta numbers ONLY. CRM-side figures are never stored — they are derived by query at read time so they cannot drift from the source of truth. |

**No new field on `Appointment`.** `ctwa_source_id` already exists and is already
populated.

---

## 6. The generation pipeline

Thirteen steps, matching the brief's §21. Each is **one prompt, one schema, one
deterministic validator** — never one mega-prompt. Small steps give per-step
retry and per-step review; a mid-run failure loses one step, not the campaign.
Persist after every step. Run the chain as a background job, following the
`regenerate_lead_magnet_async` precedent.

The tenant brief and strategy form the cached prefix for every step (§1.3).

| # | Step | Output |
|---|---|---|
| 1 | Brief — pre-fill from `TenantConfig`, detect gaps, emit explicit assumptions | `Campaign.brief` |
| 2 | Audience & psychology | `Campaign.strategy.audience` |
| 3 | Awareness level and what it implies for message depth | `Campaign.strategy.awareness` |
| 4 | Offer analysis, validated against real `TenantPriceItem` rows | `Campaign.strategy.offer` |
| 5 | Angles — three by default, deduplicated | `CampaignAngle` |
| 6 | Hooks per angle; generic-phrasing blocklist enforced at validation | `CampaignAsset(hook)` |
| 7 | Ads — short/medium/long, each a different psychological lever | `CampaignAd` |
| 8 | Creative format + why it fits the angle | `CampaignAd.creative_concept` |
| 9 | Image direction + copy-ready generation prompt | `CampaignAsset(image_prompt)` |
| 10 | Video concepts, constrained to phone-shootable | `CampaignAsset(video_script)` |
| 11 | Pre-filled message + first business reply, under the bot's voice rules | `CampaignAd.prefilled_message` / `.opening_message` |
| 12 | Qualification config + follow-up copy for the existing slots | `CampaignAsset(qualification_flow / followup)` |
| 13 | Test plan — one variable at a time | `CampaignAsset(test_plan)` |

**Narrow by default.** Three angles × three ads per run, not nine × six. §23 says
optimise for better ads, not more content, and a library nobody reads is worse
than one that fits on a screen. Expansion is a button.

---

## 7. The truth layer

"Do not invent facts, testimonials, statistics, guarantees, discounts, or
claims" cannot be met by asking the model nicely, and structured outputs only
validate SHAPE. Every asset passes a validator before it can be stored as
anything but a rejected draft.

- **Every money figure** must resolve to a real `TenantPriceItem` row for this
  tenant, or the asset is rejected.
- **Licensing / certification claims** gate on `licensed_claim_enabled` — the
  repo already ties that claim to documents on file. Extend the same pattern to
  guarantees, warranties, years-in-business.
- **Testimonials and statistics** are blocked outright unless sourced from a
  stored, attributed record.
- **Every place, name and phone number** resolves through the lead's own tenant.
  Standing repo rule — *no Homebase value may reach another tenant's customer* —
  and ad copy IS customer-facing copy.
- **No emojis** in any WhatsApp-bound string. Existing rule, mechanically
  checkable.
- **English + Shona** carries into generated conversation copy.

> **Absent means omit, never borrow.** A tenant with no tub price gets an angle
> that does not mention tub prices. A tenant with no certification gets copy
> that does not claim one. The generator degrades the way the bot already does.

---

## 8. Write-back — where generated assets enter the running bot

The section that makes this a system rather than a document generator.
Approving an asset must change what the bot DOES.

1. **Per-ad opening message.** The webhook already receives `referral` on the
   first message and passes it into `handle_text_message`. Look up `CampaignAd`
   by `source_id` there; an approved opening message becomes the first reply
   instead of the generic greeting. A lead who clicked the cost-of-delay ad gets
   an opening that continues that thought.
2. **The offer row.** Approving a campaign offer writes
   `TenantPriceItem(family='package', variant='facebook')` through the existing
   `offer_save` path — which also resyncs portfolio prices and regenerates the
   lead magnet. Ad and bot then quote the same number by construction.
3. **Qualification configuration.** The campaign selects which existing flow
   fields gate "qualified" and supplies the disqualifier rules. It does not
   invent a qualification model.
4. **Follow-up copy.** Generated variants become tenant `scripts` consumed by
   `send_followups` at the existing six CTWA touch points. **No second
   scheduler, no second cron, no second window calculation.**
5. **Pre-launch verification.** The generated opening message and qualification
   flow are written out as a scenario file and replayed through the real
   pipeline in the Scenario Lab before the ad goes live.

**The single biggest design risk:** if §12–§15 are generated as free text sitting
in a library rather than configuration the bot consumes, you get two competing
follow-up systems and copy describing a conversation the bot does not have.
Those five write-backs are what prevent it.

---

## 9. Lead quality and metrics

A campaign is not successful because WhatsApp conversations are cheap. To make
that operational, "qualified" needs a definition the database can compute.

**Qualified** = `lead_score >= 50` (two or more flow fields answered) OR booked —
AND failing none of the campaign's disqualifier rules: outside `excluded_areas`,
wrong service family, or no timeline after the full follow-up sequence.

Then, per `ctwa_source_id`:

| Metric | Source | Available |
|---|---|---|
| Spend, impressions, reach, CTR, CPC | Meta | import |
| WhatsApp conversations | `Appointment` count | free |
| Cost per conversation | spend ÷ conversations | needs spend |
| Qualified leads | `lead_score` + fit rules | free |
| Qualification rate | qualified ÷ conversations | free |
| Cost per qualified lead | spend ÷ qualified | needs spend |
| Appointments, quotes | `scheduled_datetime`, `Quotation` | free |
| Revenue, ROAS | `Job` completion | free |

The headline visual writes itself: **cost per conversation against qualification
rate**, one point per ad. Cheap-and-junk bottom-left, expensive-and-excellent
top-right. The quadrant an ad lands in IS the recommendation.

---

## 10. Diagnosis — compute it, don't ask for it

§19 asks the AI to determine whether the problem is the ad, offer, audience,
WhatsApp flow, follow-up or sales process. Handed to an LLM as an open question
that produces confident invented causes. The repo convention — prefer
deterministic resolvers, reserve the model for genuinely ambiguous language —
applies exactly here.

**Deterministic half.** Five ratios per ad, each compared to the account median:

| Ratio | A weak value means |
|---|---|
| impressions → clicks | creative or hook |
| clicks → conversations | the ad-to-WhatsApp handoff |
| conversations → qualified | targeting, or ad copy that doesn't filter |
| qualified → appointment | the bot's flow, or the offer |
| appointment → sale | pricing or the sales process |

Whichever ratio deviates furthest below median localises the problem to one
stage. That is arithmetic, and it is right.

**LLM half.** Claude receives the identified stage plus the actual assets
involved, and writes the recommendation and the specific fix.

---

## 11. Screens

A `/campaigns/` section mirroring how `/quotations/` is already built.

- `/campaigns/` — list, `paginate_by = 25` **with page controls** (the quotes
  list shipped without them and everything past row 25 was unreachable)
- `/campaigns/new/` — the wizard, pre-filled from tenant config
- `/campaigns/<pk>/` — strategy, angles, ads, asset library tabs
- `/campaigns/<pk>/ads/<pk>/` — detail, edit, regenerate, approve, export
- `/campaigns/<pk>/performance/` — the dashboard
- `/campaigns/<pk>/metrics/import/` — paste or upload Meta's export

### 11.1 Non-negotiables inherited from this repo

- Extend `bot/layouts/base.html`; add entries to BOTH `main_nav.html` and
  `mobile_nav.html`.
- `@staff_required` and tenant-scoped on every view — gate the view, never only
  the template.
- Mobile-first. Wide tables stack into cards with a `data-label` on every `<td>`;
  reuse the existing `pbq-*` responsive layer rather than inventing a second one.
- Font Awesome only — `bi bi-*` renders blank, Bootstrap Icons is never loaded.
- Every new page and every mutating action gets a case in
  `bot/test_views_actions.py`. The pre-commit hook runs that suite plus TEST 0.
- Any change to intent routing or the pricing gates — which the §8 write-back
  touches — needs an API-free TEST 0 case.

### 11.2 Owner-facing input style

The wizard is fifteen questions a busy tradesperson has to answer. Tap-to-select
chips over free text wherever the option set is known, with an "Other" escape —
the pattern `offer.py::COMMON_INCLUDES` already uses.

---

## 12. Phasing

| Phase | Scope | Note |
|---|---|---|
| **0 · Attribution** | `Campaign` + `CampaignAd` + the `ctwa_source_id` join + a report over existing leads | **Zero AI, zero Meta.** Ships value immediately: which ads produced which booked jobs, from data already in the database. |
| **1 · Generator core** | Claude client + boundary test; brief → strategy → angles → hooks → ads; asset library with edit/duplicate/regenerate/approve/archive/export | The bulk of the build. |
| **2 · Write-back** | The five wiring points in §8 | **The phase that makes it a system**, and the one most likely to be skipped under time pressure. |
| **3 · Creative** | Format recommendations, image direction, generation prompts, video concepts | Self-contained. |
| **4 · Dashboard** | Manual metrics import + the CRM-side funnel + the cheap-vs-good plot | Useful with or without Meta numbers. |
| **5 · Diagnosis** | Ratio analysis, recommendation writer, testing plan | |
| **6 · Meta API** | Marketing API OAuth, automatic metric sync | Optional, gated on App Review, deliberately last. |

One phase per working session, each ending in a commit that passes the gate.

---

## 13. Risks

- **Meta permissions.** `ads_read` and per-client ad-account access sit in the
  same App Review territory that has already been painful for WhatsApp
  onboarding. Assume months — hence Phase 6.
- **Two competing follow-up systems.** Covered by §8, but it is the failure mode
  to watch for.
- **Voice drift.** The bot's copy rules are hard-won: no emojis, script-first
  then paraphrase on re-ask, no plumber name in acknowledgements, no unprompted
  pricing. Generated WhatsApp copy must be constrained by the existing
  sales-flow rulebook, not written free-form.
- **Cost and volume.** ~US$1 per full run is fine, but it needs a per-tenant rate
  limit and a visible "generating" state, since the chain is a background job.
- **File size.** `bot/models.py` at ~2,900 lines argues for the separate
  `bot/campaigns/` package rather than appending.
- ~~DeepSeek truncation~~ — retired by the §1 provider split. It was the top
  risk while campaign generation was going to run on DeepSeek; on Claude, with
  structured outputs and a 128K output ceiling, it is not the standing threat.

---

## 14. Open decisions

1. **Whose tool is it?** Tenant-facing self-serve, or operator-only with
   campaigns run on tenants' behalf? *Recommendation:* tenant-scoped like
   quotes, with metrics import owner-gated at first — spend data is
   commercially sensitive and the operator will hold the ad accounts initially.
2. **Per-campaign WhatsApp numbers?** Would give perfect attribution without
   Meta, but multiplies Cloud API onboarding. *Recommendation:* no —
   `source_id` already gives ad-level attribution.
3. **How opinionated is the qualification override?** Letting a campaign reorder
   the bot's flow questions is powerful and risky; letting it only DEFINE the
   qualified threshold is safe. *Recommendation:* start with the threshold only.
4. **Approval gate.** Should generated ad copy require review before it can
   write back to the live bot, mirroring `TenantIntake`? *Recommendation:* yes,
   and for the same reason.

---

## 15. Conventions to follow

- **The provider boundary is load-bearing.** Claude for campaign generation,
  DeepSeek for the runtime. Enforced by test, not by discipline (§1.2).
- **Generated assets configure the bot; they never describe a second one** (§8).
- **Absent means omit, never borrow** — the truth layer degrades the way the bot
  already does (§7).
- **Prefer deterministic resolvers over LLM round-trips.** Diagnosis is
  arithmetic; the model writes prose (§10).
- **Campaign steps fail loudly.** No silent fallback, no half-generated
  campaign (§1.3).
- New per-item metadata = optional JSON keys, never new columns.
- No emojis in customer-facing copy. Support English + Shona.
- `PYTHONIOENCODING=utf-8` for local runs on Windows.
- At the end of every edit, provide a suitable `git commit -m` message.
