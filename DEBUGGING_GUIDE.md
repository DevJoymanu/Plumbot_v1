# Plumbing CRM — Debugging & System Guide

> Written for non-developers. If something breaks, start at the section that matches your symptom.

---

## Table of Contents

1. [What This System Does](#1-what-this-system-does)
2. [How the Code Is Organised](#2-how-the-code-is-organised)
3. [What Was Changed in the Restructuring](#3-what-was-changed-in-the-restructuring)
4. [How to Run a Health Check](#4-how-to-run-a-health-check)
5. [The Most Likely Things to Break — and How to Fix Them](#5-the-most-likely-things-to-break--and-how-to-fix-them)
6. [Feature Reference](#6-feature-reference)
7. [File Map — Where Is Everything?](#7-file-map--where-is-everything)
8. [Environment Variables — What Must Be Set](#8-environment-variables--what-must-be-set)
9. [Quick-Fix Command Reference](#9-quick-fix-command-reference)

---

## 1. What This System Does

This is a **WhatsApp CRM for a Zimbabwean plumbing company (Homebase Plumbers)**. Customers send WhatsApp messages; the system replies automatically, books appointments, scores leads, and notifies the team.

### The main loop

```
Customer sends WhatsApp message
        ↓
WhatsApp (Meta) sends it to your /webhook/ URL
        ↓
bot/whatsapp_webhook.py receives it
        ↓
It classifies the message (language, intent, service type)
        ↓
It calls Plumbot (the chatbot brain) to generate a reply
        ↓
Plumbot sends the reply back via WhatsApp Cloud API
        ↓
Appointment/lead record is created or updated in the database
```

The **dashboard** at `/dashboard/` lets your team see all leads, appointments, quotations, and follow-ups.

---

## 2. How the Code Is Organised

### Before the restructuring

Everything — 10,316 lines — was in one file: `bot/views.py`. It was very hard to find anything or understand what was happening.

### After the restructuring

The code is now split into focused files. Think of it like a filing cabinet where every drawer has a clear label.

```
bot/
├── views/                         ← All web page and API logic
│   ├── __init__.py                ← Master list: tells Python what lives where
│   ├── appointments.py            ← Appointment detail, update, cancel, confirm
│   ├── calendar_views.py          ← Calendar page
│   ├── dashboard.py               ← Main dashboard
│   ├── followups.py               ← Follow-up management, pause/resume bot
│   ├── jobs.py                    ← Job scheduling after site visits
│   ├── quotation_templates.py     ← Reusable quotation templates
│   ├── quotations.py              ← Create/send/edit/PDF quotations
│   ├── settings_views.py          ← App settings, WhatsApp test
│   └── plumbot/                   ← The chatbot brain (split into 8 parts)
│       ├── base.py                ← Plumbot class — just the skeleton
│       ├── state_mixin.py         ← Customer state: email, name, retry counts
│       ├── response_mixin.py      ← Generating AI replies, pricing logic
│       ├── extraction_mixin.py    ← Pulling data out of conversations
│       ├── availability_mixin.py  ← Available time slots, business hours
│       ├── booking_mixin.py       ← Booking appointments, datetime parsing
│       ├── reschedule_mixin.py    ← Detecting and handling reschedule requests
│       ├── notification_mixin.py  ← Sending confirmations and team alerts
│       └── plan_upload_mixin.py   ← Handling plan/blueprint uploads
│
├── services/
│   ├── clients.py                 ← Shared API connections (Twilio, DeepSeek)
│   └── lead_scoring.py            ← How leads are scored 1-100
│
├── utils.py                       ← Small helper functions used everywhere
├── auth_views.py                  ← Login, logout, change password
├── whatsapp_webhook.py            ← Entry point for all WhatsApp messages
├── whatsapp_cloud_api.py          ← Sends messages via WhatsApp Cloud API
├── unified_classifier.py          ← Classifies message intent in one AI call
├── out_of_scope_handler.py        ← Handles messages outside plumbing scope
├── repeated_question_detector.py  ← Detects when customer asks same thing twice
├── models.py                      ← Database structure
├── urls.py                        ← URL routing (which URL → which view)
├── decorators.py                  ← Login/staff requirement checks
└── forms.py                       ← Web form definitions
```

---

## 3. What Was Changed in the Restructuring

This section explains every change made, in plain English.

### Change 1: Created `bot/utils.py`

**What it contains:** 8 small helper functions that are used by many parts of the system.

| Function | What it does |
|---|---|
| `_to_decimal(value)` | Safely converts a number like `"US$150"` to a proper decimal |
| `_to_float(value)` | Same but returns a float |
| `_safe_logo_url()` | Returns the company logo URL without crashing |
| `_safe_logo_data_uri()` | Returns the logo as embedded data for PDF generation |
| `_reset_pk_sequence(model)` | Fixes database ID numbering after bulk deletes |
| `_append_admin_note(appointment, msg)` | Adds a timestamped note to an appointment |
| `clean_phone_number(phone)` | Converts `whatsapp:+263...` to just `263...` |
| `format_phone_number_for_storage(phone)` | Converts `263...` back to `whatsapp:+263...` |

**Why it was moved:** These were defined inside `views.py`, meaning any other file that needed them had to import from `views.py`. Now they live in `utils.py` where they belong.

**What could break:** If any file still does `from bot.views import _to_decimal` — it still works, because `views/__init__.py` re-exports it. But new code should import from `bot.utils`.

---

### Change 2: Created `bot/services/clients.py`

**What it contains:** The connections to external services.

| Variable | What it is |
|---|---|
| `twilio_client` | Connection to Twilio (SMS fallback) |
| `deepseek_client` | Connection to DeepSeek AI (generates chatbot replies) |
| `TWILIO_ACCOUNT_SID` | Twilio account ID (from environment variable) |
| `TWILIO_AUTH_TOKEN` | Twilio secret key (from environment variable) |
| `TWILIO_WHATSAPP_NUMBER` | The WhatsApp number used for SMS fallback |
| `DEEPSEEK_API_KEY` | DeepSeek API key (from environment variable) |
| `GOOGLE_CALENDAR_CREDENTIALS` | Google Calendar credentials dict |

**Why it was moved:** Before, `twilio_client` and `deepseek_client` were initialised **twice** in `views.py` (the second overwriting the first). Now there is one canonical version.

**What could break:** If an environment variable is missing (e.g. `TWILIO_AUTH_TOKEN` is empty), `Client(None, None)` will fail silently at startup and crash when first used. See [Section 8](#8-environment-variables--what-must-be-set) for the full list of required variables.

---

### Change 3: Removed duplicate `_to_dec` function

**What happened:** `views.py` had `_to_decimal` at line 72 and an identical function called `_to_dec` at line 345. The 5 places that called `_to_dec()` were updated to call `_to_decimal()` instead. `_to_dec` was deleted.

**What could break:** Nothing — the replacement was done automatically. But if you ever find old code somewhere that calls `_to_dec(...)`, rename it to `_to_decimal(...)`.

---

### Change 4: Removed duplicate auth views from `views.py`

**What happened:** `login_view`, `logout_view`, `profile_view`, and `change_password_view` existed in both `views.py` AND `auth_views.py`. The copies in `views.py` were removed.

**Why:** The version in `auth_views.py` is the correct one — it has proper security decorators (`@csrf_protect`, `@never_cache`, `@sensitive_post_parameters`). The copy in `views.py` was missing these protections.

**What could break:** Nothing — `views/__init__.py` re-exports the auth views from `auth_views.py`, so `urls.py` still works.

**Also fixed:** `auth_views.py` had `from .decorators import admin_required` but `admin_required` did not exist in `decorators.py`. This was a pre-existing bug that caused an `ImportError`. Fixed by removing the unused import.

---

### Change 5: Removed 6 duplicate functions

These duplicate functions existed in the original `views.py`. In each case, one version was kept (the more complete, more recently written one) and the other was deleted.

| Function | Deleted version | Kept version | Why |
|---|---|---|---|
| `use_template` | Line 509 (basic) | Line 591 (with `transport_cost`) | The newer one handles transport cost correctly |
| `send_followup` | Line 2163 (Twilio SMS) | Line 3093 (WhatsApp Cloud API) | The system uses WhatsApp Cloud API, not old Twilio |
| `job_appointments_list` | Line 2454 (outdated field names) | Line 2722 (correct field names + statistics) | The newer one uses the correct database field names |
| `extract_all_available_info_with_ai` | Line 3501 | Line 3796 (has "prevent re-asking" fix) | Newer version prevents asking for info already provided |
| `detect_reschedule_request_with_ai` | Line 4718 | Line 5632 (more examples, more accurate) | More complete prompt |
| `_is_delay_or_exit_signal` | Line 545 (basic) | Line 594 (enhanced) | More keyword coverage |

**What could break:** If any template or external code called the Twilio version of `send_followup` specifically — it won't work. But the WhatsApp version is the active one and does the same thing.

---

### Change 6: Split `views.py` into the `views/` package

The 10,316-line `views.py` was converted into a **package** (a folder called `views/` containing many files). The file `views/__init__.py` re-exports every name so that `urls.py` does not need to change.

**What could break:** Any new code that imports from `bot.views` directly will still work. But if you add a new view function, you must:
1. Add it to the correct module file (e.g. `appointments.py`)
2. Add it to `views/__init__.py` if `urls.py` needs to use it
3. Add a URL pattern in `urls.py`

---

### Change 7: Split Plumbot into 8 mixins

The `Plumbot` class (6,376 lines, 120 methods) was split into 8 mixin classes. Python's **multiple inheritance** means `Plumbot` inherits all methods from all 8 mixins. Every `self.some_method()` call inside any mixin still works because `self` is always the full `Plumbot` object.

| Mixin | Methods | What it handles |
|---|---|---|
| `StateMixin` | 21 | Tracking customer state: email captured?, name declined?, retry count |
| `ResponseMixin` | 35 | Generating AI replies, pricing responses, delay detection |
| `ExtractionMixin` | 10 | Pulling area, date, name, service from conversation |
| `AvailabilityMixin` | 13 | Available time slots, business hours, date formatting |
| `BookingMixin` | 11 | Actually booking appointments, parsing date/time from messages |
| `RescheduleMixin` | 7 | Detecting and handling reschedule requests |
| `NotificationMixin` | 5 | Sending confirmation messages, team alerts, Google Calendar |
| `PlanUploadMixin` | 12 | Handling customer plan/blueprint uploads |

**What could break:**
- If a method is referenced that no longer exists (was deleted as a duplicate), you get `AttributeError: 'Plumbot' object has no attribute 'X'`.
- If two mixins define a method with the same name, Python uses the **first one in the MRO** (Method Resolution Order). The order is: `StateMixin → ResponseMixin → ExtractionMixin → AvailabilityMixin → BookingMixin → RescheduleMixin → NotificationMixin → PlanUploadMixin`.

---

## 4. How to Run a Health Check

Run this from the project root folder on the server:

```bash
python manage.py check
```

**Expected output** (healthy):
```
System check identified 1 issue (0 silenced).
WARNINGS:
?: (urls.W005) URL namespace 'admin' isn't unique.
```
The admin namespace warning is pre-existing and harmless — it has always been there.

**If you see `ImportError` or `ModuleNotFoundError`** — go to [Section 5, Problem 1](#problem-1-importerror-or-modulenotfounderror).

**If you see `OperationalError: no such table`** — run `python manage.py migrate`.

**Check that all URLs resolve:**
```bash
python manage.py show_urls
```
Every URL in the list should map to a real function, not `None`.

---

## 5. The Most Likely Things to Break — and How to Fix Them

---

### Problem 1: `ImportError` or `ModuleNotFoundError`

**Symptom:** The server won't start. You see something like:
```
ImportError: cannot import name 'some_function' from 'bot.views'
```
or
```
ModuleNotFoundError: No module named 'bot.views.models'
```

**Most likely causes and fixes:**

**Cause A: A new function was added to `views.py` directly (old habit)**
After the restructuring, `views.py` no longer exists. If someone created a new function and put it in the root of the `bot/` folder as `views.py`, it would conflict with the `views/` package.
```
Fix: Delete bot/views.py if it exists. Put new views in the correct file inside bot/views/.
```

**Cause B: A file inside `bot/views/` uses two-dot relative imports instead of three**
Files inside `bot/views/` are one level deeper than `bot/`. To import models, they need `from ..models`, not `from .models`.
Files inside `bot/views/plumbot/` are two levels deeper and need `from ...models`.
```
# WRONG (from inside bot/views/something.py):
from .models import Appointment

# CORRECT:
from ..models import Appointment

# WRONG (from inside bot/views/plumbot/something.py):
from ..models import Appointment

# CORRECT:
from ...models import Appointment
```

**Cause C: A new export was not added to `views/__init__.py`**
If you add a new view function but don't add it to `bot/views/__init__.py`, then `urls.py` can't find it.
```
Fix: Open bot/views/__init__.py and add the import.
Example: from .appointments import my_new_view
```

---

### Problem 2: WhatsApp messages come in but get no reply

**Symptom:** Customer sends a WhatsApp message, nothing happens, no reply.

**Step 1 — Check the webhook is receiving messages:**
Look at Railway logs. You should see lines like:
```
Received WhatsApp webhook POST
Processing message from whatsapp:+263...
```
If you see nothing, the webhook URL is wrong or Meta is not sending to your server.

**Step 2 — Check the webhook verification:**
Go to Meta Business → WhatsApp → Webhook. The verify token must match `WHATSAPP_VERIFY_TOKEN` in your environment variables.

**Step 3 — Check DeepSeek is working:**
The AI response generation can fail silently if the API key is wrong or the DeepSeek service is down.
```
# In Railway logs, look for:
DeepSeek API error: ...
Translation error (DeepSeek): ...
```
If DeepSeek is down, the bot falls back to hardcoded responses for common questions but may go silent on complex ones.

**Step 4 — Check the phone number format:**
All phone numbers in the database are stored as `whatsapp:+263xxxxxxxxx`. If a number is stored differently, the bot can't find the appointment record and won't reply.
```python
# Run in Django shell (python manage.py shell):
from bot.models import Appointment
Appointment.objects.filter(phone_number__startswith='263').count()
# Should be 0 — if not, those numbers are stored in the wrong format
```

---

### Problem 3: Bot replies in English even though customer wrote in Shona

**Symptom:** Customer writes in Shona, bot replies in English.

**How Shona detection works:**
1. The message goes through `unified_classifier.py` which tells the bot the language
2. After the bot generates an English reply, `_translate_reply_for_customer()` in `whatsapp_webhook.py` calls DeepSeek to translate it to Shona

**Most likely causes:**

**Cause A: DeepSeek API call failed**
If DeepSeek is slow or erroring, translation is skipped and English is sent.
```
Look for in logs: "Translation error (DeepSeek): ..."
```

**Cause B: Shona keywords not recognised**
The keyword detector in `repeated_question_detector.py` → `detect_language_simple()` has a list of Shona marker words. If the customer uses a Shona phrase not in the list, it defaults to English.
```
File: bot/repeated_question_detector.py
Function: detect_language_simple()
Fix: Add the unrecognised Shona word to the shona_markers list
```

**Cause C: Customer's message looks like English to the AI**
Short messages like "ok" or "yes" are ambiguous — the AI classifier may mark them as English. This is correct behaviour.

---

### Problem 4: Quotation PDF generation fails

**Symptom:** Clicking "Send Quotation" shows an error, or the PDF is blank/broken.

**Most likely cause: ReportLab not installed**
```bash
pip install reportlab
```
The code wraps ReportLab in a `try/except ImportError` so it won't crash on import, but PDF generation will silently fail.

**Second cause: Logo file missing**
The PDF includes the company logo. If `bot/static/images/logo.jpg` or `bot/static/logo.jpg` does not exist, the logo is skipped (not an error). If it crashes, check:
```
File: bot/utils.py
Function: _safe_logo_data_uri()
```

**Third cause: Decimal conversion failing**
If a quotation item has a price entered as text (e.g. `"N/A"` instead of `0`), `_to_decimal()` returns `0.00` silently. The quotation total will be wrong but it won't crash.

---

### Problem 5: `AttributeError: 'Plumbot' object has no attribute 'X'`

**Symptom:** Bot crashes when processing a WhatsApp message. Railway logs show:
```
AttributeError: 'Plumbot' object has no attribute 'some_method_name'
```

**This means a Plumbot method is calling another method that no longer exists** (was deleted as a duplicate during the restructuring).

**How to fix:**
1. Note the missing method name from the error
2. Search for it in the original duplicate list in [Section 3, Change 5](#change-5-removed-6-duplicate-functions)
3. Find the replacement method name and update the call

**Most likely deleted names that might still be referenced somewhere:**
| Missing name | Use this instead | In file |
|---|---|---|
| `_to_dec(...)` | `_to_decimal(...)` | `bot/utils.py` |
| `_is_delay_or_exit_signal` (old version) | Same name, but now in `ResponseMixin` | `bot/views/plumbot/response_mixin.py` |
| `detect_reschedule_request_with_ai` (old) | Same name, now in `RescheduleMixin` | `bot/views/plumbot/reschedule_mixin.py` |

**How to find which mixin a method is in:**
```python
# Run in Django shell:
from bot.views import Plumbot
import inspect
print(inspect.getfile(Plumbot.method_name))
```

---

### Problem 6: Login page shows `500 Internal Server Error`

**Most likely cause: Missing environment variable**
`auth_views.py` and the database both need environment variables. If `SECRET_KEY` or `DATABASE_URL` is missing, Django crashes on login.

```bash
# Quick check — run on server:
python manage.py check
```
If it says `ImproperlyConfigured`, an environment variable is missing. See [Section 8](#8-environment-variables--what-must-be-set).

**Second cause: Database not migrated**
```bash
python manage.py migrate
```

---

### Problem 7: Google Calendar appointments not appearing

**Symptom:** Booking confirmation is sent to customer but appointment doesn't appear in Google Calendar.

**The Google Calendar integration lives in:**
```
bot/views/plumbot/notification_mixin.py → add_to_google_calendar()
```

**Most likely causes:**

1. `GOOGLE_CALENDAR_CREDENTIALS` is empty (`{}`) — the credentials JSON was never loaded from environment variables. Check `bot/services/clients.py` and your Railway environment variables.
2. The service account email has not been given access to the calendar.
3. The `GOOGLE_CALENDAR_ID` environment variable is missing.

**How to test without a customer:**
```bash
python manage.py shell
>>> from bot.models import Appointment
>>> a = Appointment.objects.latest('created_at')
>>> from bot.views import Plumbot
>>> p = Plumbot(a.phone_number)
>>> p.add_to_google_calendar()
```

---

### Problem 8: Follow-up messages not sending automatically

**How auto-follow-ups work:**
A Django cron job (`django_cron`) runs `send_followups` management command on a schedule. It finds leads that are overdue for a follow-up and sends a WhatsApp message.

**Check if cron is running:**
```bash
python manage.py runcrons --force
```
If it errors, check the logs. The most common cause is a database issue or a missing environment variable.

**Check which leads are due:**
```bash
python manage.py shell
>>> from bot.models import Appointment
>>> from django.utils import timezone
>>> Appointment.objects.filter(follow_up_status='pending', is_lead_active=True).count()
```

**Manually trigger follow-ups:**
Go to `/followups/check/` in the dashboard, or:
```bash
python manage.py send_followups
```

---

### Problem 9: `send_portfolio_to_lead` or `send_image_to_lead` returns 404

**Symptom:** Clicking "Send Portfolio" on the appointment page gives a 404 error.

**Why this might happen:**
After the restructuring, `send_image_to_lead` and `send_portfolio_to_lead` were moved to `bot/views/followups.py`. The `urls.py` uses `views.send_image_to_lead` (via the `views` module reference), which resolves through `views/__init__.py` → `followups.py`.

If the URL pattern exists but the function can't be imported, Django would show a 500 not a 404. A true 404 means the URL pattern is missing.

```
Check: bot/urls.py should have:
path('appointments/<int:pk>/send-image/', views.send_image_to_lead, ...)
path('appointments/<int:pk>/send-portfolio/', views.send_portfolio_to_lead, ...)
```

---

## 6. Feature Reference

### WhatsApp Chatbot (Plumbot)

The chatbot handles the full customer journey automatically:

| Stage | What happens | Method/file |
|---|---|---|
| First message | Appointment record created | `whatsapp_webhook.py` |
| Service detection | Identifies bathroom/kitchen/pipe/etc. | `unified_classifier.py` |
| Info collection | Asks for area, date, time | `response_mixin.py → generate_response()` |
| Plan upload | Asks if customer has a blueprint | `plan_upload_mixin.py` |
| Booking | Creates calendar event, sends confirmation | `booking_mixin.py → book_appointment()` |
| Follow-up | Sends message if lead goes cold | `management/commands/send_followups.py` |
| Reschedule | Detects "can we move it?" and handles | `reschedule_mixin.py` |
| Out-of-scope | Redirects non-plumbing queries | `out_of_scope_handler.py` |
| Shona translation | Translates bot reply if customer writes Shona | `whatsapp_webhook.py → _translate_reply_for_customer()` |

### Quotation System

| Feature | Where it lives | URL |
|---|---|---|
| Create quotation | `views/quotations.py → CreateQuotationView` | `/appointments/<id>/create-quotation/` |
| Create standalone (no appointment) | `views/quotation_templates.py → StandaloneQuotationView` | `/quotations/new/` |
| Generate PDF | `views/quotations.py → build_quotation_pdf_file()` | Called by `send_quotation` |
| Send to customer via WhatsApp | `views/quotations.py → send_quotation()` | `/quotations/<id>/send/` |
| Quotation templates | `views/quotation_templates.py` | `/templates/` |

### Lead Scoring

Leads are automatically scored 0–100 based on:
- Information completeness (area provided, service type identified, etc.)
- Response behaviour (how quickly customer replies)
- Project type (renovation scores higher than drain unblocking)

```
File: bot/services/lead_scoring.py
Called by: whatsapp_webhook.py after each incoming message
```

### Dashboard

| Section | URL | What it shows |
|---|---|---|
| Main dashboard | `/dashboard/` | Today's appointments, hot leads, recent activity |
| Priority leads | `/leads/priority/` | Leads scored HOT or VERY HOT |
| Follow-ups | `/followups/` | Leads awaiting follow-up contact |
| Jobs | `/jobs/` | Scheduled job appointments |
| Calendar | `/calendar/` | Visual calendar of all appointments |

### Language Support (Shona + English)

- **Detection:** `repeated_question_detector.py → detect_language_simple()` — keyword-based, no API call needed
- **Translation:** `whatsapp_webhook.py → _translate_reply_for_customer()` — uses DeepSeek AI
- **Classifier context:** `unified_classifier.py` — tells the AI to expect Shona/English/mixed
- **Hardcoded Shona pricing:** `views/plumbot/response_mixin.py → handle_service_inquiry()` — pricing already written in Shona so no translation needed

---

## 7. File Map — Where Is Everything?

| If you need to change... | Edit this file |
|---|---|
| How the bot responds to messages | `bot/views/plumbot/response_mixin.py` |
| How appointments are booked | `bot/views/plumbot/booking_mixin.py` |
| How rescheduling works | `bot/views/plumbot/reschedule_mixin.py` |
| WhatsApp confirmation messages | `bot/views/plumbot/notification_mixin.py` |
| Plan upload conversation flow | `bot/views/plumbot/plan_upload_mixin.py` |
| Available time slot logic | `bot/views/plumbot/availability_mixin.py` |
| Customer state tracking (email, name, retries) | `bot/views/plumbot/state_mixin.py` |
| Data extraction from conversations | `bot/views/plumbot/extraction_mixin.py` |
| Shona translation prompt | `bot/whatsapp_webhook.py` → `_translate_reply_for_customer()` |
| Shona keyword detection | `bot/repeated_question_detector.py` → `detect_language_simple()` |
| Out-of-scope / delay handling | `bot/out_of_scope_handler.py` |
| Repeat question detection | `bot/repeated_question_detector.py` |
| Quotation PDF layout | `bot/views/quotations.py` → `build_quotation_pdf_file()` |
| Dashboard data | `bot/views/dashboard.py` |
| Appointment list/detail pages | `bot/views/appointments.py` |
| Job scheduling pages | `bot/views/jobs.py` |
| Follow-up management | `bot/views/followups.py` |
| Login / logout | `bot/auth_views.py` |
| URL routing | `bot/urls.py` |
| Database models / fields | `bot/models.py` |
| API connections (Twilio, DeepSeek) | `bot/services/clients.py` |
| Small helper functions | `bot/utils.py` |
| Scheduled tasks (cron) | `bot/management/commands/` |

---

## 8. Environment Variables — What Must Be Set

All of these must be set in Railway (or your `.env` file for local development). If any is missing, the corresponding feature will fail.

| Variable | Used for | Where it's used |
|---|---|---|
| `SECRET_KEY` | Django security | `settings.py` |
| `DATABASE_URL` | PostgreSQL connection | `settings.py` |
| `TWILIO_ACCOUNT_SID` | Twilio SMS (fallback) | `bot/services/clients.py` |
| `TWILIO_AUTH_TOKEN` | Twilio SMS (fallback) | `bot/services/clients.py` |
| `TWILIO_WHATSAPP_NUMBER` | Sending SMS | `bot/services/clients.py` |
| `DEEPSEEK_API_KEY` | All AI responses | `bot/services/clients.py` |
| `WHATSAPP_ACCESS_TOKEN` | Sending WhatsApp messages | `bot/whatsapp_cloud_api.py` |
| `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp sender identity | `bot/whatsapp_cloud_api.py` |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verification with Meta | `bot/whatsapp_webhook.py` |
| `SENDGRID_API_KEY` | Sending emails | `bot/customer_emails.py` |
| `GOOGLE_CALENDAR_ID` | Which calendar to add events to | `bot/views/plumbot/notification_mixin.py` |
| `DEBUG` | `False` in production | `settings.py` |
| `ALLOWED_HOSTS` | Railway domain | `settings.py` |

**How to check if a variable is set (on the server):**
```bash
python manage.py shell
>>> import os
>>> print(os.environ.get('DEEPSEEK_API_KEY', 'NOT SET'))
```

---

## 9. Quick-Fix Command Reference

Run all commands from the project root folder.

```bash
# Check if Django is healthy
python manage.py check

# Apply any pending database changes
python manage.py migrate

# Collect static files (after deploying)
python manage.py collectstatic --noinput

# Send follow-up messages manually
python manage.py send_followups

# Send appointment reminders manually
python manage.py send_reminders

# Open Django interactive shell (for debugging)
python manage.py shell

# Check all URL patterns resolve
python manage.py show_urls

# Start the development server locally
python manage.py runserver
```

**In the Django shell — useful one-liners:**

```python
# Find an appointment by phone number
from bot.models import Appointment
a = Appointment.objects.get(phone_number='whatsapp:+263774xxxxxx')

# Check what Plumbot would do with a message (without sending it)
from bot.views import Plumbot
p = Plumbot('whatsapp:+263774xxxxxx')
reply = p.generate_response('Ndoda kubhukisha', [])
print(reply)

# See all hot leads
Appointment.objects.filter(lead_status__in=['hot', 'very_hot'], is_lead_active=True)

# Verify all mixin methods are accessible
from bot.views import Plumbot
print([m for m in dir(Plumbot) if not m.startswith('_')])

# Check which mixin a method comes from
import inspect
from bot.views import Plumbot
print(inspect.getfile(Plumbot.book_appointment))
```

---

*Last updated after restructuring: May 2026. If something breaks that isn't covered here, run `python manage.py check` first, then look at the Railway logs for the exact error message — copy that message and search this document for the relevant keyword.*
