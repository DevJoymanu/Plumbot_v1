# Plumbot Multi-Tenant Plan & Architecture

Turning the single-company Plumbot (Homebase Plumbers, hardwired) into a platform
where each plumbing company is an isolated **tenant** with its own WhatsApp number,
prices, scripts, staff logins, and dashboard — on the existing stack (Django +
Postgres on Railway, Meta WhatsApp Cloud API, DeepSeek). No new framework
dependencies.

---

## 1. Tenancy model decision

**Row-level tenancy in one shared Postgres schema** — a `Tenant` model and a
`tenant` foreign key on every business table, enforced by scoped managers and
middleware.

Why not the alternatives:

| Option | Verdict |
|---|---|
| Schema-per-tenant (`django-tenants`) | New dependency (violates house rule), migration pain on Railway, overkill below ~50 tenants |
| Database-per-tenant | Operationally heavy on Railway; crons/dashboards would need N connections |
| **Row-level FK (chosen)** | No new deps, one migration path, crons/scenario-lab/exports keep working, isolation enforced in code + testable |

The trade-off of row-level tenancy is that isolation is only as good as query
discipline. We mitigate with: a required `tenant` FK (not nullable after
backfill), default-scoped managers, middleware that pins `request.tenant`, and a
dedicated isolation test block in TEST 0 (see §9).

---

## 2. Current-state audit — what is hardwired to Homebase today

This is the real work. Everything below must move from code into per-tenant data.

### 2.1 Identity & credentials (env-level, one set)
- `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_BUSINESS_ACCOUNT_ID`, `WHATSAPP_VERIFY_TOKEN` (`bot/whatsapp_cloud_api.py:115-121`)
- SendGrid/Brevo sender identity, `from_name="Takudzwa"` (`bot/customer_emails.py`)
- `PREVIOUS_WORK_IMAGE_URLS` env (portfolio gallery, `bot/whatsapp_webhook.py:42`)
- Google Calendar integration (notification_mixin)

### 2.2 Business facts baked into code
- **Prices** — `structured_pricing` tables, `_FAMILY_PRICE_COMPONENTS`, `_FAMILY_ROUGH_PRICE`, `_FAMILY_LABOUR_BREAKDOWN`, `_FAMILY_FLAT_PRICE`, `_FREESTANDING_TUB_*`, Facebook package US$800, tub US$670/US$160 (all in `bot/views/plumbot/response_mixin.py`), plus every price echoed inside DeepSeek prompts (`_answer_standalone_question`, `generate_dynamic_response`)
- **Sizes** — `_TUB_SIZE_BLOCKS`
- **Location/FAQ facts** — "Hatfield, Harare", hours "Sunday–Friday 8–6", licensed/registered (`bot/faq.py::_FACTS`)
- **People** — plumber **Takudzwa**, `+263774819901` fallback (identity handler, handoff reply, FAQ, email signatures, inbound-email auto-replies)
- **Service area** — Zimbabwe-wide with a decline list (Gweru, Bulawayo, …) in `StateMixin._is_excluded_city_keywords` + the AI service-area prompt
- **Sales profile** — `bot/sales_profiles/homebase.md` (source of truth for figures)
- **Portfolio** — `bot/portfolio_catalog.py` static list + image URLs
- **Scripts** — opener, description question, area question, availability script, tie-down banks, delay-flow copy: all string constants in `response_mixin.py` / `out_of_scope_handler.py`
- **Booking rules** — closed Saturdays, business hours (availability_mixin)

### 2.3 Data with no tenant column
`Appointment`, `WhatsAppInboundEvent`, `ScheduledFollowup`, `ScheduledReminder`,
`AppointmentNote`, `Job`, `Quotation(+Item/Template)`, `ServiceArea`,
`TestScenario`, `ConversationMessage`.

### 2.4 Cross-tenant machinery
- Crons (`send_followups`, `send_reminders`,
  `summarize_unconfirmed_leads`, `process_inbound_emails`) iterate **all** leads
- Dashboard/appointments/priority views query `Appointment.objects` globally
  (staff flag is the only gate — all staff see everything)
- Webhook: one endpoint, no routing — every inbound is assumed Homebase
- Scenario Lab & test console: global; 999-test-lines shared
- `IMAP [APT-{id}]` email-reply matching: global appointment ids (fine — ids stay
  globally unique)

---

## 3. Target architecture

```
                     Meta WhatsApp Cloud API
                    (one webhook URL, N numbers)
                               │
                               ▼
                 POST /webhook/  ──► resolve tenant by
                                     payload metadata.phone_number_id
                               │
                     ┌─────────┴──────────┐
                     │  TenantContext      │  (request-scoped / turn-scoped)
                     │  tenant, profile,   │
                     │  wa_client, prices  │
                     └─────────┬──────────┘
                               ▼
        handle_text_message(tenant, sender, …)  ← same pipeline as today,
                               │                  reading TenantProfile instead
                               ▼                  of constants
        Appointment(tenant=…)  · conversation_history · crons scoped per tenant
                               │
                               ▼
        Dashboard: request.tenant from logged-in user’s membership
        (platform admin = tenant switcher; client staff = their tenant only)
```

### 3.1 New models (one migration, `bot/models.py`)

```python
class Tenant(models.Model):
    name          = CharField(unique=True)          # "Homebase Plumbers"
    slug          = SlugField(unique=True)          # "homebase"
    is_active     = BooleanField(default=True)
    created_at    = DateTimeField(auto_now_add=True)

class TenantWhatsAppChannel(models.Model):          # 1–N per tenant (usually 1)
    tenant             = FK(Tenant)
    phone_number_id    = CharField(unique=True)     # ← webhook routing key
    business_account_id= CharField()
    access_token       = TextField()                # encrypted at rest (Fernet w/ SECRET_KEY-derived key)
    verify_token       = CharField()
    display_number     = CharField()

class TenantProfile(models.Model):                  # 1:1 — everything from §2.2
    tenant            = OneToOneField(Tenant)
    # identity
    plumber_name      = CharField()                 # "Takudzwa"
    plumber_contact   = CharField()                 # "+263774819901"
    location_line     = CharField()                 # "We're in Hatfield, Harare."
    business_hours    = JSONField()                 # {"days": "Sun–Fri", "open": "08:00", "close": "18:00", "closed": ["sat"]}
    excluded_areas    = JSONField(default=list)     # decline list
    # sales
    currency          = CharField(default="US$")
    packages          = JSONField(default=list)     # [{"name":"Facebook package","price":800,"contents":"freestanding tub + side chamber"}]
    sales_profile_md  = TextField()                 # replaces sales_profiles/<slug>.md
    faq_facts         = JSONField(default=dict)     # replaces faq._FACTS
    scripts           = JSONField(default=dict)     # opener/description/area/availability first-pass scripts (falls back to platform defaults)
    # email
    email_from_name   = CharField()
    email_sender      = EmailField()

class TenantPriceItem(models.Model):                # replaces every price table
    tenant   = FK(Tenant)
    family   = CharField()                          # shower / tub / geyser / …
    variant  = CharField(blank=True)                # built_in / freestanding / corner
    label    = CharField()                          # "shower cubicle"
    supply   = DecimalField(null=True)
    labour   = DecimalField(null=True)
    flat     = DecimalField(null=True)              # basin-style single figure
    sizes    = JSONField(default=list)              # measurement blocks
    keywords = JSONField(default=list)

class TenantPortfolioItem(models.Model):            # replaces portfolio_catalog.py
    tenant, title, from_price, description, keywords, image_url

class TenantMembership(models.Model):               # user → tenant
    user   = FK(auth.User)
    tenant = FK(Tenant)
    role   = CharField(choices=[("owner",…),("staff",…)])
# Platform admin = user.is_superuser → tenant switcher, sees all.
```

Plus `tenant = FK(Tenant)` added to every table in §2.3.

### 3.2 Tenant resolution

**Inbound (webhook):** Meta sends `entry[].changes[].value.metadata.phone_number_id`
in every event. `whatsapp_webhook` looks up `TenantWhatsAppChannel` by that id →
builds a `TenantContext` → passes it down. Unknown id → 200-and-log (never 4xx
Meta). Verify-token check per channel.

**Outbound:** `WhatsAppCloudAPI` stops reading env in `__init__` and takes the
channel: `WhatsAppCloudAPI(channel)`. A per-tenant client cache avoids re-reading
the DB per send. Env vars remain as the seed values for the Homebase channel row.

**Dashboard:** middleware sets `request.tenant` from `TenantMembership` (or the
session-selected tenant for superusers). A `TenantScopedListView` mixin +
`Appointment.objects.for_tenant(t)` replace raw queries. `staff_required` gains a
sibling `tenant_member_required`.

**Crons:** each command wraps its body in `for tenant in Tenant.objects.filter(is_active=True):`
and scopes every queryset. Per-tenant failures are isolated (`try/except` per
tenant so one tenant's bad data can't stop another's follow-ups).

### 3.3 Config access layer — the seam that makes this tractable

One new module, `bot/tenant_config.py`:

```python
class TenantConfig:                    # cached per turn
    def __init__(self, tenant): ...
    # everything the mixins hardcode today:
    price(family, variant=None) -> PriceItem
    price_components() -> {family: (supply, labour)}   # feeds _FAMILY_* tables
    size_blocks() -> {variant: text}
    faq_fact(topic) -> str
    script(key) -> str                 # 'opener', 'description_q', 'area_q', …
    plumber_name / plumber_contact / location_line / hours_line
    packages() / excluded_areas() / portfolio_items()
    prompt_context() -> str            # rendered block injected into DeepSeek prompts
```

The mixins change from `return "All good, what area are you in?"` to
`return self.cfg.script('area_q')` — where `self.cfg` defaults to the **platform
default config** (today's exact strings/prices) when a tenant hasn't overridden
them. That default-fallback rule is what keeps the migration incremental: nothing
changes behaviourally until a tenant row overrides it.

**Nullability rule (decided 2026-07-15, see CLIENT_ONBOARDING_CHECKLIST.md):**
every tenant config field/upload is nullable. Fallback is two-class: **generic
copy** (openers, question scripts, tie-downs — no business facts) may fall back
to platform defaults; **business facts** (names, numbers, prices, locations,
licensed claims, portfolio) have NO fallback — absent means graceful omission
(deflect price to the free site visit, never claim licensed without docs on
file, no photo offers without a portfolio). A missing fact must never resolve
to another tenant's (or Homebase's) value. The Homebase tenant is simply the
one tenant whose config happens to be fully populated by the seed migration.
Functional go-live minimum: trading name + WhatsApp number.

DeepSeek prompts get their business blocks (services, pricing guide, company
info) from `cfg.prompt_context()` instead of inline literals.

### 3.4 Platform (developer) dashboard

The operator's console — superuser-gated, separate from tenant staff dashboards,
under its own URL namespace (e.g. `/platform/`), reusing the existing dashboard
templates/auth/design system. Grows with the phases:

- **Tenant operations (Phase 3 deliverable — prerequisite for a second tenant):**
  tenant list (status, channel health, lead/booking counts), create/deactivate,
  **impersonation** (enter any tenant's dashboard as they see it — also the
  support tool), and the tenant config editor (profile/prices/scripts/FAQ). While
  config editing is admin-only (open decision #2), this console is the *only*
  place tenant config gets edited.
- **Health & monitoring (Phase 4):** per-tenant WhatsApp channel status (last
  inbound/outbound, token validity, webhook errors), a surface for
  unknown-`phone_number_id` 200-and-log events (else a misconfigured tenant fails
  silently), per-tenant cron run board (the per-tenant try/except isolation is
  only useful if failures are visible), send/email-processing failures.
- **Onboarding & quality (Phase 5):** the new-tenant wizard lives here; per-tenant
  golden scenario pack results (a tenant goes live only when their pack is green);
  per-tenant 999 test-line management.
- **Usage & billing (Phase 6):** DeepSeek token metering per tenant/day, message
  volumes, leads→bookings conversion, subscription state (follows decision #4).

### 3.5 What stays shared
- DeepSeek API key (platform-level; add `tenant` to a usage log for billing later)
- The classifier/dispatcher logic, unified classifier, delay flows, all TEST 0-pinned behaviour — these are platform IP, identical across tenants
- Email transport (Brevo/SendGrid keys) — per-tenant *sender identity* only
- The `999…` test-line convention (test lines get a tenant too, so Scenario Lab runs per tenant profile)

---

## 4. Phased migration plan

Each phase deploys independently; Homebase keeps working throughout because the
default config == today's constants.

### Phase 0 — Foundations (schema + backfill) · ~2–3 sessions
1. Add `Tenant`, `TenantWhatsAppChannel`, `TenantProfile`, `TenantMembership`; add **nullable** `tenant` FK to all §2.3 tables.
2. Data migration: create tenant `homebase` from current env values + `sales_profiles/homebase.md`; backfill `tenant_id` on every row; then a second migration makes the FK non-null.
3. `LeadQuerySet.for_tenant(t)`; middleware setting `request.tenant`; superuser tenant switcher in the navbar.
4. **Isolation TEST 0 block**: create two tenants in-memory, assert `for_tenant` never leaks across (see §9).

*Exit criterion: production identical, every row owned by `homebase`.*

### Phase 1 — Webhook routing + outbound credentials · ~2 sessions
1. Webhook resolves channel by `phone_number_id`; thread `tenant` through `handle_text_message` → `_generate_and_schedule_reply` → `Plumbot(phone, tenant=…)` (the shared-Appointment-instance rule from `[[shared-appointment-instance]]` already gives us the single seam).
2. `WhatsAppCloudAPI(channel)` + client cache; `delayed_response`/media senders take the tenant’s client.
3. Fernet-encrypt `access_token` at rest.
4. Scenario runner & test console pass an explicit tenant (default `homebase`).

*Exit criterion: a second sandbox WhatsApp number routes to a second test tenant end-to-end.*

### Phase 2 — Config extraction (the big one) · ~4–6 sessions, in slices
Extract in this order, each slice with its own TEST 0 additions:
1. **Identity strings** — plumber name/contact, location, hours → `TenantProfile` (touches identity handler, handoff, FAQ, emails, prompts)
2. **Prices & sizes** — `TenantPriceItem` feeding the existing `_FAMILY_*` dict shapes via `TenantConfig` (the dict shapes stay; only their source changes — TEST 0 price pins keep passing against the homebase seed)
3. **FAQ facts + scripts** — `faq_facts` / `scripts` JSON with platform-default fallback
4. **Prompt blocks** — `cfg.prompt_context()` into every DeepSeek prompt
5. **Portfolio** — `TenantPortfolioItem` + per-tenant image URLs; gallery/catalogue senders read it
6. **Service area** — per-tenant excluded list feeding both the keyword check and the AI prompt

*Exit criterion: `grep -rn "Takudzwa\|Hatfield\|US\$670\|263774819901" bot/ --include="*.py"` returns only the seed migration and platform defaults.*

### Phase 3 — Dashboard & roles · ~2 sessions
1. Scope every view (appointments, dashboard, priority, calendar, jobs, quotations, exports, test-leads, Scenario Lab) through `request.tenant`.
2. Roles: `owner` (billing/settings), `staff` (leads); platform superadmin.
3. Per-tenant branding on the dashboard (name/logo).
4. A **Tenant Settings page** (staff-only, owner role): edit profile, prices, scripts, FAQ — this replaces "ask the dev to change a constant".
5. **Platform console v1** (§3.4) under `/platform/`: tenant list, create/deactivate, impersonation, config editor — prerequisite for running a second tenant.

### Phase 4 — Crons, email, notifications · ~1–2 sessions
1. Wrap all five crons in the per-tenant loop with per-tenant error isolation
   (`notify_priority_leads` was removed 2026-07-08).
2. Emails: sender name/signature/templates from `TenantProfile`; `[APT-{id}]` matching unchanged (ids are global).
3. Plumber notifications → `tenant.profile.plumber_contact`; Google Calendar credentials become per-tenant (or off for tenants without it).
4. Platform console health pages (§3.4): channel status, cron run board, unknown-`phone_number_id` log, send/email failures per tenant.

### Phase 5 — Onboarding + per-tenant Scenario Lab · ~2 sessions
1. "New tenant" wizard (superadmin): name → WhatsApp channel creds → profile → price sheet import (CSV or copy-from-default) → seed FAQ/scripts from platform defaults.
2. Scenario Lab: scenarios get a `tenant` FK; the runner executes against that tenant's config; seed a **golden scenario pack** (the current 21) cloned per tenant on creation — instant regression coverage for every new company.
3. `manage.py chat --tenant <slug>`.

### Phase 6 — Hardening & commercial hooks · ongoing
- Per-tenant DeepSeek usage metering (tokens/turn logged with tenant id) → billing
- Rate limiting per channel; Meta app review considerations for multiple numbers
- Backup/export per tenant; tenant off-boarding (deactivate → archive → delete)
- Optional: per-tenant language defaults (Shona/English/Ndebele mixes)

---

## 5. Request flows after migration

**Inbound message**
```
Meta POST /webhook/
  → verify per-channel token
  → channel = TenantWhatsAppChannel[phone_number_id]     (404→log+200)
  → ctx = TenantContext(channel.tenant)                  (config cache, wa client)
  → handle_text_message(ctx, sender, text, wamid)
      → Appointment.objects.for_tenant(ctx.tenant).get_or_create(phone=…)
      → pipeline unchanged; every string/price via ctx.cfg
      → sends via ctx.wa_client (tenant's number)
```

**Dashboard request**
```
login → TenantMembership → request.tenant (superuser: session-selected)
  → every ListView/detail/action: .for_tenant(request.tenant)
  → object-level check on detail/update views (tenant mismatch → 404)
```

---

## 6. Data isolation rules (non-negotiable)

1. `tenant` FK non-null on every business table after Phase 0.
2. Views never call `Appointment.objects.filter(...)` without `.for_tenant()` —
   enforced by a light grep-based CI check (`git grep -n "objects.filter" bot/views | grep -v for_tenant` allow-list).
3. Detail/update/delete views 404 on tenant mismatch (never 403 — don't leak existence).
4. Phone numbers are unique **per tenant**, not globally (`unique_together(tenant, phone_number)`) — the same customer can talk to two companies.
5. WAMID dedup keyed `(tenant, wamid)`.
6. Test lines (999) also carry a tenant; the Test Leads page and purge are tenant-scoped.

---

## 7. What does NOT change

- The conversation pipeline, classifier-then-dispatch architecture, delay flows,
  pricing gates, script-first rule, two-message split, WAMID/quote machinery —
  all platform behaviour, shared by every tenant.
- TEST 0 gate + pre-commit + CI: unchanged and still the merge gate.
- Railway deployment shape (one service, one Postgres). Scale-out later is
  horizontal (more gunicorn workers) — nothing in this plan shards the DB.

---

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| A missed hardcode ships Homebase's price/name to another tenant | Phase-2 exit grep + per-tenant golden scenario pack must pass before a tenant goes live |
| Query without tenant scope leaks data | Non-null FK, `for_tenant` discipline + CI grep, isolation tests in TEST 0 |
| Meta credential handling (tokens in DB) | Fernet encryption at rest, masked in admin, never logged |
| Cron blast radius (one tenant's error stops all) | Per-tenant try/except + per-tenant summary logging |
| Prompt drift per tenant (custom scripts break pinned behaviour) | Tenant overrides limited to *content* slots (names, prices, facts); flow logic and gate-pinned scripts stay platform-level, override-able only via reviewed defaults |
| Two instances of an Appointment per turn (stale writes) | Already fixed — single instance per turn; keep the rule for tenant context too (one `TenantContext` per turn) |

---

## 9. Testing strategy

- **TEST 0 additions per phase**: tenant resolution (phone_number_id → tenant, unknown id safe), `for_tenant` isolation (two tenants, zero leakage), config fallback (tenant without overrides == platform defaults, byte-identical scripts/prices), per-tenant price lookup replaces `_FAMILY_*` correctly.
- **Scenario Lab per tenant**: the 21-scenario golden pack runs against `homebase` unchanged (regression), and against a synthetic `acme-plumbing` tenant with different prices/names — assertions parameterized (`expect: {plumber_name}` template support in the runner is a small Phase-5 addition).
- **The grep gates**: hardcode-leak grep (Phase 2 exit) and unscoped-query grep (§6.2) wired into CI next to the existing gate job.

---

## 10. Effort summary

| Phase | Scope | Estimate |
|---|---|---|
| 0 | Tenant schema, backfill, scoping primitives | 2–3 sessions |
| 1 | Webhook routing, per-tenant WhatsApp creds | 2 |
| 2 | Config extraction (prices/scripts/FAQ/prompts/portfolio) | 4–6 |
| 3 | Dashboard scoping, roles, settings page | 2 |
| 4 | Crons, email, notifications | 1–2 |
| 5 | Onboarding wizard, per-tenant Scenario Lab | 2 |
| 6 | Hardening, metering, billing hooks | ongoing |

Phases 0–1 make the system *structurally* multi-tenant with Homebase as the only
tenant. Phase 2 is the bulk of the work and the point of no return for
hardcodes. A second paying tenant can realistically onboard after Phase 3, with
Phases 4–5 running in parallel with their pilot.

---

## 11. Open decisions — RESOLVED 2026-07-15

1. **Same customer, two tenants** — ✅ Yes: two independent leads; uniqueness is per-tenant.
2. **Who edits tenant config** — ✅ Admin-only. Additionally: admin can send an owner a
   **config intake form** (profile, prices, scripts, FAQ) that the owner fills in;
   submissions land as a *pending draft* the admin reviews and approves before any of
   it becomes the tenant's live config. (Implementation: a token-linked form page +
   draft-vs-live config states + an approve/reject step in the platform console —
   lands with Phase 3/5.)
3. **WhatsApp numbers** — ✅ **REVISED 2026-07-15: platform-provisioned.** Client numbers
   are registered under the platform's already-verified Meta Business Manager,
   permanently (supersedes the earlier bring-your-own choice; see §13 for the model's
   mechanics and accepted trade-offs). Why: onboarding drops from ~2 weeks of client
   Meta verification to days (number registration + display-name review), and clients
   need zero Meta setup. Consequences accepted: the platform pays every tenant's Meta
   conversation charges (must be priced into the flat monthly fee — see decision #4),
   messaging quality rating pools across tenants (per-tenant quality monitoring in the
   console becomes important), and clients don't own their numbers. Phase 1 simplifies:
   one platform system-user token can cover all numbers; `TenantWhatsAppChannel` keeps
   per-tenant `phone_number_id` (still the webhook routing key) with the token shared
   or per-channel as convenient.
4. **Billing model** — ✅ Flat monthly subscription per tenant, **but** per-tenant cost
   metering still ships in Phase 6: DeepSeek tokens per turn, WhatsApp message counts,
   email sends — logged with tenant id and surfaced in the platform console, so margins
   per tenant are visible and a future usage-based tier is possible without rework.

---

## 12. AGREED LAUNCH SCOPE — FULL-SCOPE launch (locked 2026-07-15, supersedes earlier de-scoped version)

The first external client onboards as soon as Phases 0–5 are **all complete**.
Nothing is deferred past launch. The initial launch includes:

1. **Onboarding wizard + owner intake form** — admin sends the owner a form
   link; owner fills in profile/prices/scripts/FAQ; submission lands as a
   pending draft; admin verifies and approves before anything goes live
   (decision #2). **Client #1 onboards through this wizard** — the first client
   is the end-to-end test of the machine every future client uses.
2. **Full Phase 4** — per-tenant cron loop with error isolation, per-tenant
   email identity, plumber notifications from `TenantProfile`.
3. **Full platform console** — tenant CRUD, impersonation, config editor +
   draft approval, channel health boards, cron run board.
4. **Per-tenant Scenario Lab** — the 21-scenario golden pack cloned
   automatically for every new tenant; **hard go-live gate:** a client does not
   go live until their pack passes against their own prices and name.

**Timeline (from 2026-07-15):** 14–18 sessions total. At 1 session/day →
go-live ~day 16–18 (~2026-08-01). Hitting day 14 requires ~4–5 double-session
days and best-case Phase 2. Day 7 is not achievable at full scope.

**Build order:** Days 1–2 Step 0 + Phase 0 · Days 3–4 Phase 1 (+ sandbox
number proof) · Days 5–9 Phase 2 · Days 10–12 Phase 3 (dashboard scoping,
roles, console CRUD + impersonation) · Days 13–14 Phase 4 (cron loop, email,
notifications, health/cron boards) · Days 15–16 Phase 5 (wizard, intake
draft/approve flow, per-tenant Scenario Lab + golden-pack cloning) · Days
16–18 onboard client #1 through the wizard, run their golden pack, live
number test, go-live with the rollback runbook standing by.

**External clocks (start day 1):** with platform-provisioned numbers (decision
#3 revised — client numbers live under the platform's verified BM), there is
**no client-side Meta verification wait**. Remaining external items: acquire/
register the client's number under the platform WABA + display-name review
(days), and the client's content (price sheet, services, area, plumber
name/contact, FAQ, portfolio photos, hours) — collectable early via a
checklist doc, then entered through the intake form once it exists. The launch
date is now purely code-bound.

**Pace assumption:** ~1 session/day, an evening deploy window most days, the
phone test after each deploy. Slippage in Phase 2 (the big one) moves the
date directly.

---

## 13. The CHOSEN "one Business Manager" model (decision #3, revised 2026-07-15)

The platform's already-verified Meta Business Manager owns the WhatsApp Business
Account(s); a phone number is registered/bought per tenant under it; one platform
system-user token covers all numbers. Webhook routing by `phone_number_id` is
unchanged. Onboarding a tenant's number = register number under the WABA +
display-name review (days, not weeks — no per-client business verification).

**Accepted trade-offs (eyes open):**
- The platform is the payer for any Meta charges — but under current bot behaviour
  the expected Meta bill is **~US$0 per tenant**: Meta only charges for *template*
  messages, and Plumbot sends exclusively free-form messages inside the customer's
  open 24-hour service window (free, unlimited; CTWA opens a free 72h window).
  Cost appears only if template messages are introduced (re-engagement after the
  window closes, out-of-window appointment reminders) — a few US cents per
  delivered template, payable by the platform → price into the flat fee when that
  feature ships. Per-tenant message metering (Phase 6) keeps it visible.
  *Related audit (independent of tenancy):* free-form sends outside the 24h window
  are NOT delivered — `send_reminders` likely only reaches customers with a
  recently open window; reliable reminders need a (paid) utility template.
- Messaging quality rating and messaging-limit tiers pool at the WABA level → one
  spammy tenant can degrade all tenants on that WABA. Mitigations: per-tenant
  quality/limit monitoring on the console health board (Phase 4), and the option
  to spread tenants across multiple WABAs under the same BM as we grow.
- Per-WABA number caps exist → more WABAs under the same BM when needed.
- Tenants don't own their numbers; leaving the platform means losing the number
  (or a negotiated WABA-to-WABA migration). Make this explicit in client
  contracts to keep it good faith.

**Rejected alternative (for the record):** bring-your-own Meta Business — each
tenant verifies their own business (~2 weeks), owns their number and billing.
Cleanest ownership, but onboarding waits on Meta reviewing a third party's
paperwork, and clients must handle Meta setup themselves. Revisit if a large
client demands number ownership — the schema (`TenantWhatsAppChannel` with
per-channel token) already supports mixing both models per tenant.
