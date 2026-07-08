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

DeepSeek prompts get their business blocks (services, pricing guide, company
info) from `cfg.prompt_context()` instead of inline literals.

### 3.4 What stays shared
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

### Phase 4 — Crons, email, notifications · ~1–2 sessions
1. Wrap all six crons in the per-tenant loop with per-tenant error isolation.
2. Emails: sender name/signature/templates from `TenantProfile`; `[APT-{id}]` matching unchanged (ids are global).
3. Plumber notifications → `tenant.profile.plumber_contact`; Google Calendar credentials become per-tenant (or off for tenants without it).

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

## 11. Open decisions (need a call before Phase 0)

1. **Same customer, two tenants** — confirmed OK to treat as two independent leads? (Plan assumes yes: uniqueness is per-tenant.)
2. **Who edits tenant config** — client owners self-serve (Phase 3 settings page) or platform-admin only at first? (Plan assumes admin-only until the settings UI is hardened.)
3. **WhatsApp numbers** — will new tenants bring their own Meta Business + number (each needs its own access token/app review), or will the platform provision numbers under one Business Manager? Affects Phase 1 credential shape only.
4. **Billing model** — per-lead, per-booking, or flat monthly? Phase 6 metering design follows from this; nothing earlier blocks on it.
