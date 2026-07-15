# Client Onboarding Checklist — everything a new tenant must provide

Derived from a sweep of every Homebase-specific value in the codebase
(2026-07-15; 124 hardcoded occurrences across 27 files). This list is the
contract for the **owner intake form** (§12 launch scope): every item below
becomes a form field or upload, and nothing outside this list is needed from
the client. The "used by" notes are for us, not the client.

## Nullability rule (decided 2026-07-15): EVERY item is optional

All intake fields and uploads are **nullable at the schema level** — a tenant
can be created, saved as a draft, and even go live with gaps. What "absent"
means is defined per item and is always **graceful omission — NEVER a fallback
to another tenant's (or Homebase's) value**:

- **Missing price / service** → the bot does not quote a figure; it deflects to
  the free site-visit quote ("exact price confirmed on site").
- **Missing licensed/certification docs** → the bot never claims licensed or
  registered (see 1.9 — the claim is gated on proof).
- **Missing portfolio** → no photo offers; portfolio/gallery paths disabled.
- **Missing decline list** → all areas serviceable.
- **Missing hours** → no hours claims; availability uses platform-default slots.
- **Missing guarantee / payment / duration facts** → that FAQ topic answers
  generically or hands off to the plumber instead of stating a fact.
- **Missing business WhatsApp (1.5)** → email buttons use the direct line (1.4).
- **Missing email sender** → customer emails disabled for that tenant.
- **Missing Google Calendar** → calendar sync off (already optional today).

Two-class fallback rule for implementers: **generic copy** (openers, question
scripts, tie-downs) may fall back to platform defaults — they mention no
business facts. **Business facts** (names, numbers, prices, locations, claims)
have no fallback: absent = the bot stays silent on it.

*Functional minimum to actually go live* (not a schema constraint): trading
name (1.1) + a WhatsApp number (6.1) — the bot cannot exist without an
identity and a channel. Everything else can be filled in while live.

## 1. Business identity

| # | Item | Example (Homebase) | Used by |
|---|---|---|---|
| 1.1 | Trading name | Homebase Plumbers | greetings, FAQ, emails, dashboard branding |
| 1.2 | Base location (suburb + city) | Hatfield, Harare | FAQ "where are you", prompts |
| 1.3 | Owner/lead plumber name | Takudzwa | FAQ contact, handoffs, email signature/from-name |
| 1.4 | Plumber direct line (calls) | +263774819901 | FAQ contact, handoff replies, email Call button, plumber alerts |
| 1.5 | Business WhatsApp number (may differ from 1.4) | +263776255077 | email WhatsApp buttons, reminder emails |
| 1.6 | Business hours + closed days | Sun–Fri 8am–6pm, closed Sat | FAQ, availability slots, booking validation |
| 1.7 | Languages spoken | English + Shona | reply language mirroring |
| 1.8 | Licensed/registered? (yes/no + approved wording) | "Fully licensed and registered" | FAQ, objection handling |
| 1.9 | **Certifications & proof documents** — plumbing trade certificates, business registration certificate, any professional-body memberships; insurance/liability cover if held | scans/photos (PDF or image) | (a) admin due-diligence during intake approval — the bot never claims "licensed" unless proof is on file; (b) the "credentials available on request" promise — sendable to a customer who asks; (c) attachable to quotations/emails |
| 1.10 | Logo (optional) | — | dashboard branding, quotation/email header |

## 2. Service area

| # | Item | Example | Used by |
|---|---|---|---|
| 2.1 | Areas served (default: whole country, mobile) | Zimbabwe-wide, they travel | area qualification |
| 2.2 | Decline list — places they will NOT travel to | Gweru, Bulawayo, Mutare, Masvingo, Vic Falls, Hwange, Beitbridge, Plumtree | keyword check + AI service-area prompt |
| 2.3 | Timezone | Africa/Johannesburg | reminders, follow-up scheduling, emails |

## 3. Services & full price sheet *(the big one)*

For **every** service they offer, in their currency (default US$), marked
either **all-in** or **labour + parts separate**, as "from" rates:

| # | Item | Example | Used by |
|---|---|---|---|
| 3.1 | Renovation prices | Bathroom from US$900, Kitchen from US$600 | pricing replies, prompts |
| 3.2 | Package deals (name + contents + price) | Full Bathroom Package US$800; Facebook Package (freestanding tub + side chamber) US$800 | package pitches |
| 3.3 | Individual fittings (all-in) | Shower cubicle US$170, vanity US$180, toilet seat & cistern US$70, basin US$70, wall-hung toilet/chamber US$160, standard tub US$160, freestanding tub US$670+ | per-item pricing, portfolio captions |
| 3.4 | Supply-vs-labour splits where relevant | wall-hung toilet: US$130 supply + US$30 install | combined quotes, labour breakdowns |
| 3.5 | Size/measurement variants | standard tub 1500×700 | size Q&A |
| 3.6 | Geyser services | supply+install US$160, replacement US$350, valve US$25, thermostat US$30, element US$40 | pricing replies |
| 3.7 | Repairs & maintenance menu | leaking tap US$15, drain unblocking US$20/US$50, jetting US$80, burst pipe US$40, … | pricing replies |
| 3.8 | Currency | US$ | all price rendering |

## 4. Sales facts & policies

| # | Item | Example | Used by |
|---|---|---|---|
| 4.1 | Free site visit / quote? + wording | Yes — free visit, fixed price on the spot | core close, objection handling |
| 4.2 | Payment methods | Cash, EcoCash, bank transfer | FAQ payment |
| 4.3 | Deposit policy | none currently — owner sign-off needed to add | quote copy |
| 4.4 | Guarantee wording | "Satisfaction guaranteed on every job" | risk-reversal copy |
| 4.5 | Typical job durations | small repair = hours; full reno = a few days | FAQ job_duration |
| 4.6 | Top 3 objections + approved answers (optional — platform defaults exist) | free quote? / hidden costs? / licensed? | objection pre-emption |

## 5. Portfolio (previous work)

| # | Item | Example | Used by |
|---|---|---|---|
| 5.1 | 10–20 photos of completed jobs (good quality, portrait or landscape) | previous_work_photos/*.jpeg | gallery sends, catalogue |
| 5.2 | Per photo: what it is + which service/price it maps to | "Modern Open-Plan Kitchen — kitchen reno from US$600" | photo captions, quoted-photo pricing |
| 5.3 | Per photo: 1–2 sentence back-story (where, for whom, outcome) — we can draft from bullet points, client approves | "Young family in Borrowdale wanted the kitchen to be the heart of the home…" | contextual replies about a specific photo |
| 5.4 | Portfolio PDF (optional — we can generate one) | pre-designed PDF emailed to leads | delay-quote email attachment |

## 6. Channel & accounts

| # | Item | Example | Used by |
|---|---|---|---|
| 6.1 | A phone number for the bot's WhatsApp (SIM/virtual; we register it under the platform Business Manager — decision #3) | — | the bot's identity |
| 6.2 | WhatsApp display name (Meta reviews it, days) | "Homebase Plumbers" | what customers see |
| 6.3 | Email address to send customer emails FROM (or we provision one) + from-name | Takudzwa <…> | reminder/confirmation emails |
| 6.4 | Where plumber alerts go (WhatsApp number) | usually = 1.4 | new-booking notifications |
| 6.5 | Google Calendar for bookings (optional) | — | calendar sync, or off |
| 6.6 | Staff logins: name + email per dashboard user, who is "owner" | — | TenantMembership roles |

## 7. Commercial

| # | Item | Notes |
|---|---|---|
| 7.1 | Signed agreement incl. number-ownership clause | the WhatsApp number is registered under the platform's Business Manager (decision #3) — state it plainly |
| 7.2 | Flat monthly fee agreed | decision #4 |

---

**Not needed from the client** (platform-level, identical for every tenant):
conversation flow and qualification logic, follow-up cadence, delay/objection
handling, the DeepSeek prompts and classifiers, WAMID dedup, email transport
(Brevo/SendGrid), dashboards, Scenario Lab. Clients supply *facts and assets*;
the platform supplies the *brain*.

**Fastest path for the client:** items 1–4 are one sitting with whoever owns
pricing (60–90 min, or just their existing price list + a phone call); item 5
is a WhatsApp dump of photos + a voice note per photo; item 6 is IT-admin
stuff we walk them through.
