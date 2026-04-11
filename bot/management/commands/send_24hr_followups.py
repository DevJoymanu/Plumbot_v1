"""
Django Management Command: send_24hr_followups
================================================
Sends context-aware follow-up messages to leads whose WhatsApp 24-hour window
is STILL OPEN, subject to time-of-day contact windows.

Run every 30 minutes via Railway Scheduler / cron:
    python manage.py send_24hr_followups

══════════════════════════════════════════════════════════════════
CONTACT WINDOWS  (Africa/Johannesburg — SAST UTC+2)
══════════════════════════════════════════════════════════════════

  Window A  ·  Morning     08:00 – 10:00   "start of day, high open rates"
  Window B  ·  Lunch       12:00 – 14:00   "decision-making window"
  Window C  ·  After-work  17:00 – 20:00   "peak WhatsApp hours"

Messages are ONLY sent during one of these three windows.
Outside a window, the command logs the lead as "deferred" and
re-evaluates on the next run once a window opens.

URGENCY OVERRIDE
────────────────
If a lead has < 3 hours left in their WhatsApp 24-hour window AND
at least one attempt is still remaining today, the contact-window gate
is bypassed so we never lose the lead due to a timing gap.
This prevents the scenario where a window closes at 11 PM and we
couldn't send because it was outside contact hours.

══════════════════════════════════════════════════════════════════
ATTEMPT SCHEDULE  (hours of silence required before each attempt)
══════════════════════════════════════════════════════════════════

  Attempt 1  ·   2 h  — warm, value-first opener
  Attempt 2  ·   5 h  — social proof / light urgency
  Attempt 3  ·  10 h  — ultra-short "9-word" final nudge

Max 3 attempts per calendar day per lead.
Min 90 minutes of silence before any attempt fires.
Attempt counts are stored in internal_notes (no migration needed).

══════════════════════════════════════════════════════════════════
CONVERSATION STAGES
══════════════════════════════════════════════════════════════════

  quote_sent           Quotation sent, awaiting decision
  images_viewed        Work photos sent, customer went quiet
  appointment_pending  All details collected, not confirmed yet
  site_visit_booked    Appointment confirmed — light reminder only
  plan_requested       Bot asked for plan upload, not received
  pricing_asked        Pricing discussed, no next step taken
  intake_stalled       First question(s) asked, not answered
  generic              Fallback for any other stall

══════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import os
import re
from datetime import timedelta
from typing import Optional

import pytz
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from openai import OpenAI

from bot.models import Appointment
from bot.whatsapp_cloud_api import whatsapp_api
from bot.whatsapp_window import (
    filter_queryset_by_window,
    hours_remaining,
    is_window_open,
)

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

SA_TZ = pytz.timezone("Africa/Johannesburg")

# Contact windows: list of (start_hour_inclusive, end_hour_exclusive) in SAST
# A message may be sent if the current local hour falls in ANY window.
CONTACT_WINDOWS: list[tuple[int, int]] = [
    (8,  10),   # Window A — Morning
    (12, 14),   # Window B — Lunch
    (17, 20),   # Window C — After-work / evening
]

# Hours of customer silence required before each attempt fires (0-indexed)
ATTEMPT_THRESHOLDS_HOURS: list[float] = [2.0, 5.0, 10.0]

# Never send more than this many follow-ups per lead per calendar day
MAX_DAILY_ATTEMPTS: int = 3

# Minimum silence before ANY attempt, regardless of threshold
# (avoids firing on someone who just stopped mid-conversation)
MIN_SILENCE_MINUTES: int = 90

# If this many hours or fewer remain in the WhatsApp window, bypass the
# contact-window gate so we never lose a lead due to timing constraints.
URGENCY_WINDOW_HOURS: float = 3.0

# ── DeepSeek client ────────────────────────────────────────────────────────────
_DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")
_deepseek = (
    OpenAI(api_key=_DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")
    if _DEEPSEEK_KEY
    else None
)


# ══════════════════════════════════════════════════════════════════════════════
# CONTACT WINDOW HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _now_sast() -> "datetime":
    return timezone.now().astimezone(SA_TZ)


def _in_contact_window(now_local=None) -> bool:
    """Return True if the current SAST hour falls inside any contact window."""
    hour = (now_local or _now_sast()).hour
    return any(start <= hour < end for start, end in CONTACT_WINDOWS)


def _next_window_opens_in(now_local=None) -> str:
    """Return a human-readable string: how long until the next window opens."""
    from datetime import datetime as dt
    local = now_local or _now_sast()
    hour = local.hour

    for start, end in CONTACT_WINDOWS:
        if hour < start:
            delta_hours = start - hour
            return f"~{delta_hours}h (next window opens at {start:02d}:00 SAST)"

    # All windows passed today — next is first window tomorrow
    first_start = CONTACT_WINDOWS[0][0]
    delta_hours = 24 - hour + first_start
    return f"~{delta_hours}h (next window opens tomorrow at {first_start:02d}:00 SAST)"


def _is_urgent(appointment: Appointment) -> bool:
    """
    True when the WhatsApp 24-hour window will close within URGENCY_WINDOW_HOURS.
    Used to bypass the contact-window gate for last-chance follow-ups.
    """
    return hours_remaining(appointment) <= URGENCY_WINDOW_HOURS


def _label_window(now_local=None) -> str:
    hour = (now_local or _now_sast()).hour
    for start, end in CONTACT_WINDOWS:
        if start <= hour < end:
            labels = {8: "Morning", 12: "Lunch", 17: "After-work"}
            return labels.get(start, f"{start:02d}:00–{end:02d}:00")
    return "Outside window"


# ══════════════════════════════════════════════════════════════════════════════
# ATTEMPT TRACKING  (stored in internal_notes — no migration needed)
# ══════════════════════════════════════════════════════════════════════════════

def _today_key() -> str:
    return timezone.now().astimezone(SA_TZ).strftime("%Y-%m-%d")


def _get_todays_attempt_count(appointment: Appointment) -> int:
    today = _today_key()
    notes = appointment.internal_notes or ""
    match = re.search(rf"\[24HR_FU:{re.escape(today)}\]:(\d+)", notes)
    return int(match.group(1)) if match else 0


def _increment_attempt(appointment: Appointment) -> int:
    """Persist +1 attempt for today. Returns the new count."""
    today = _today_key()
    notes = appointment.internal_notes or ""
    key_pattern = rf"\[24HR_FU:{re.escape(today)}\]:(\d+)"
    match = re.search(key_pattern, notes)

    if match:
        old = int(match.group(1))
        new = old + 1
        new_notes = re.sub(key_pattern, f"[24HR_FU:{today}]:{new}", notes)
    else:
        ts = _now_sast().strftime("%H:%M")
        new = 1
        new_notes = f"{notes}\n[24HR_FU:{today}]:1 first={ts}".strip()

    appointment.internal_notes = new_notes
    appointment.save(update_fields=["internal_notes"])
    return new


# ══════════════════════════════════════════════════════════════════════════════
# STAGE DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def _detect_stage(appointment: Appointment) -> str:
    """Classify the conversation stage (most-specific first)."""
    history = appointment.conversation_history or []

    # 1. Quotation sent and awaiting decision?
    try:
        if appointment.quotations.filter(status="sent").exists():
            return "quote_sent"
    except Exception:
        pass

    # 2. Previous work photos sent, customer silent since?
    photos_at = getattr(appointment, "previous_work_photos_sent_at", None)
    if photos_at:
        last_in = appointment.last_customer_response
        if not last_in or last_in < photos_at:
            return "images_viewed"

    # 3. Appointment confirmed?
    if appointment.status == "confirmed" and appointment.scheduled_datetime:
        return "site_visit_booked"

    # 4. All booking fields present but not confirmed?
    if (
        appointment.project_type
        and appointment.customer_area
        and appointment.scheduled_datetime
        and appointment.status != "confirmed"
    ):
        return "appointment_pending"

    # 5. Plan requested but not received?
    if (
        appointment.has_plan is True
        and not appointment.plan_file
        and appointment.plan_status in ("pending_upload", None, "")
    ):
        return "plan_requested"

    # 6. Pricing discussed, no next step?
    sent_pricing = bool(getattr(appointment, "sent_pricing_intents", None)) or bool(
        getattr(appointment, "pricing_overview_sent", False)
    )
    if sent_pricing and not appointment.scheduled_datetime:
        return "pricing_asked"

    # 7. Bot has asked at least one real question?
    skip = ("[AUTO", "[MANUAL", "[BULK", "[PLAN", "[24HR", "[Sent ", "[FILE", "[VIDEO")
    has_bot_msg = any(
        m.get("role") == "assistant"
        and not any((m.get("content") or "").startswith(p) for p in skip)
        for m in history
    )
    if has_bot_msg:
        return "intake_stalled"

    return "generic"


# ══════════════════════════════════════════════════════════════════════════════
# TIMING ELIGIBILITY
# ══════════════════════════════════════════════════════════════════════════════

def _hours_silent(appointment: Appointment) -> float:
    ref = appointment.last_customer_response or appointment.created_at
    return (timezone.now() - ref).total_seconds() / 3600


def _attempt_is_due(appointment: Appointment, count_today: int) -> bool:
    """True when the next attempt's silence threshold has been reached."""
    if count_today >= len(ATTEMPT_THRESHOLDS_HOURS):
        return False
    required = ATTEMPT_THRESHOLDS_HOURS[count_today]
    return _hours_silent(appointment) >= required


# ══════════════════════════════════════════════════════════════════════════════
# QUERYSET
# ══════════════════════════════════════════════════════════════════════════════

def _get_eligible_leads():
    silence_cutoff = timezone.now() - timedelta(minutes=MIN_SILENCE_MINUTES)

    qs = (
        Appointment.objects
        .filter(
            is_lead_active=True,
            status__in=["pending", "in_progress"],
            chatbot_paused=False,
        )
        .exclude(plan_status__in=["plan_uploaded", "plan_reviewed", "ready_to_book"])
        .filter(
            Q(last_customer_response__isnull=False) | Q(last_inbound_at__isnull=False)
        )
        .filter(
            Q(last_customer_response__lte=silence_cutoff)
            | Q(last_customer_response__isnull=True)
        )
    )
    return filter_queryset_by_window(qs).order_by("last_customer_response")


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def _service_label(appointment: Appointment) -> str:
    mapping = {
        "bathroom_renovation":       "bathroom renovation",
        "kitchen_renovation":        "kitchen renovation",
        "new_plumbing_installation": "new plumbing installation",
        "Bathroom Renovation":       "bathroom renovation",
        "Kitchen Renovation":        "kitchen renovation",
        "New Plumbing Installation": "new plumbing installation",
    }
    return mapping.get(appointment.project_type or "", "plumbing project")


def _conversation_snippet(appointment: Appointment, n: int = 8) -> str:
    history = appointment.conversation_history or []
    skip = ("[AUTO", "[MANUAL", "[BULK", "[24HR", "[Sent ", "[FILE", "[VIDEO")
    lines = []
    for msg in history[-n:]:
        content = (msg.get("content") or "").strip()
        if not content or any(content.startswith(p) for p in skip):
            continue
        role = "Customer" if msg.get("role") == "user" else "Bot"
        lines.append(f"{role}: {content[:200]}")
    return "\n".join(lines) if lines else "No prior conversation."


def _last_bot_question(appointment: Appointment) -> str:
    for msg in reversed(appointment.conversation_history or []):
        if msg.get("role") != "assistant":
            continue
        content = (msg.get("content") or "").strip()
        if content.startswith("["):
            continue
        if "?" in content:
            return content[:350]
    return ""


def _template_message(appointment: Appointment, stage: str, attempt: int) -> str:
    """Hardcoded fallback messages — used when DeepSeek is unavailable."""
    service  = _service_label(appointment)
    area     = appointment.customer_area or ""
    name     = appointment.customer_name or ""
    greeting = f"Hi {name}," if name else "Hi there,"
    plumber  = (
        getattr(appointment, "plumber_contact_number", None) or "+263774819901"
    ).replace("+", "").replace("whatsapp:", "")

    bank: dict[str, list[str]] = {
        "quote_sent": [
            (
                f"{greeting} did you get a chance to look over the quotation? "
                f"Happy to answer any questions about it."
            ),
            (
                f"{greeting} did the numbers make sense? Our plumber can walk you "
                f"through the details if anything looks unclear."
            ),
            f"Still interested in the {service}? Just say the word.",
        ],
        "images_viewed": [
            (
                f"{greeting} did any of those photos catch your eye? "
                f"We can do a similar finish for your {service}."
            ),
            (
                f"{greeting} those were from a job we finished recently"
                f"{f' in {area}' if area else ''}. "
                f"Want us to come through and show you what's possible in your space?"
            ),
            "Still thinking? No rush — just say go when you're ready.",
        ],
        "appointment_pending": [
            (
                f"{greeting} you're all set on our side — just need your confirmation "
                f"to lock in the slot. Shall I go ahead?"
            ),
            (
                f"{greeting} your details are ready. The slot won't stay open forever — "
                f"want me to confirm it now?"
            ),
            "Confirm the visit?",
        ],
        "site_visit_booked": [
            (
                f"{greeting} your site visit is confirmed. Our plumber will call you "
                f"30 minutes before arrival. Any questions before then?"
            ),
            (
                f"{greeting} looking forward to seeing you! If anything changes, "
                f"just let us know. Contact: {plumber}"
            ),
            f"See you soon! Call us anytime on {plumber}.",
        ],
        "plan_requested": [
            (
                f"{greeting} whenever you get a chance, send through the plan for your "
                f"{service} and the plumber will review it straight away. 📐 "
                f"A clear photo is fine."
            ),
            (
                f"{greeting} once you send the plan, we can lock in your appointment "
                f"straight away. Even a phone photo of the drawing works."
            ),
            (
                f"Send the plan whenever you're ready — or we can do a free site visit "
                f"instead if that's easier."
            ),
        ],
        "pricing_asked": [
            (
                f"{greeting} hope the pricing made sense! The exact figure depends on "
                f"your setup — a free site visit locks it in with no surprises. "
                f"Want to book one?"
            ),
            (
                f"{greeting} the best way to get a fixed price is a quick on-site look. "
                f"Free, takes about an hour, fixed quote on the spot. What day works?"
            ),
            "Free site visit — shall I book one?",
        ],
        "intake_stalled": [
            (
                f"{greeting} picking up from where we left off — which service were "
                f"you after? Bathroom renovation, kitchen renovation, or new plumbing "
                f"installation?"
            ),
            f"{greeting} still happy to help — which service did you need?",
            "Still need help with your plumbing?",
        ],
        "generic": [
            (
                f"{greeting} still looking to get the "
                f"{service} sorted? Happy to help."
            ),
            f"{greeting} happy to help whenever you're ready. Any questions?",
            "Still keen? Just say the word. 😊",
        ],
    }

    options = bank.get(stage, bank["generic"])
    return options[min(attempt - 1, len(options) - 1)]


def _ai_message(
    appointment: Appointment,
    stage: str,
    attempt: int,
    template: str,
    window_label: str,
    is_urgent: bool,
) -> str:
    """DeepSeek-enhanced message, faithful to the template."""
    if not _deepseek:
        return template

    service    = _service_label(appointment)
    area       = appointment.customer_area or "not yet shared"
    silent_h   = _hours_silent(appointment)
    hrs_left   = hours_remaining(appointment)
    snippet    = _conversation_snippet(appointment)
    last_q     = _last_bot_question(appointment)

    urgency_note = (
        f"⚠️ URGENT — only {hrs_left:.1f}h left in the WhatsApp window. "
        f"This is the final chance to reach this customer. Make it count but keep it natural."
        if is_urgent
        else ""
    )

    length_rule = (
        "1 sentence only — ultra-short, 9-word style. No emoji."
        if attempt >= 3
        else "2–3 sentences. At most one emoji."
    )

    prompt = f"""You are writing a WhatsApp follow-up for Homebase Plumbers (Zimbabwe/South Africa).

CONVERSATION CONTEXT:
{snippet}

LAST UNANSWERED BOT QUESTION:
{last_q or "None"}

FOLLOW-UP DETAILS:
- Conversation stage: {stage}
- Service: {service}
- Area: {area}
- Customer silent for: {silent_h:.1f}h
- Contact window: {window_label}
- Attempt #{attempt} of 3 today
- WhatsApp window remaining: {hrs_left:.1f}h
{urgency_note}

BASE TEMPLATE (stay close to this):
\"\"\"{template}\"\"\"

RULES — every one must be followed:
1. Open with "Hi there," — never use or ask for the customer's name
2. Same intent as the template — rephrase lightly for freshness only
3. One question maximum
4. {length_rule}
5. SA/ZW English: "sorted", "keen", "sharp", "no worries"
6. Zero markdown, zero bold, zero bullet points
7. Never say: "just checking in", "following up", "hope you're well", "touching base", "circling back"
8. Sound like a real person texting, not a bot

Output ONLY the message text. No labels, no quotes, no explanation."""

    try:
        resp = _deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You write short WhatsApp follow-up messages for a plumbing company. "
                        "Stay faithful to the template. Sound human. "
                        "Open with 'Hi there,'. Never use or ask for the customer's name."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.42,
            max_tokens=180,
        )
        msg = resp.choices[0].message.content.strip()
        return msg.replace("**", "").replace("__", "")
    except Exception as exc:
        logger.warning("DeepSeek follow-up failed for lead %s: %s", appointment.id, exc)
        return template


def _generate(
    appointment: Appointment,
    stage: str,
    attempt: int,
    window_label: str,
    is_urgent: bool,
) -> dict:
    template = _template_message(appointment, stage, attempt)
    if _deepseek:
        try:
            ai_msg = _ai_message(appointment, stage, attempt, template, window_label, is_urgent)
            return {"message": ai_msg, "ai": True}
        except Exception as exc:
            logger.warning("AI generation error for %s: %s", appointment.id, exc)
    return {"message": template, "ai": False}


# ══════════════════════════════════════════════════════════════════════════════
# MANAGEMENT COMMAND
# ══════════════════════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = (
        "Send context-aware 24-hour follow-ups during SAST contact windows. "
        "Urgency override fires outside windows when < 3h remain in the WA window. "
        "Safe to run every 30 minutes — fully idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Preview messages without sending.",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Ignore contact-window gate (for testing / manual runs).",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Max leads to process per run (0 = unlimited).",
        )
        parser.add_argument(
            "--stage", type=str, default="",
            help="Only process leads matching this stage (for testing).",
        )

    # ── Entry point ───────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        dry_run      = options["dry_run"]
        force        = options["force"]
        limit        = options["limit"]
        stage_filter = options.get("stage", "").strip()

        now_local    = _now_sast()
        in_window    = _in_contact_window(now_local)
        window_label = _label_window(now_local)

        self.stdout.write(
            self.style.SUCCESS(
                f"\n{'═'*62}\n"
                f"  24-HR FOLLOW-UP  ·  {now_local.strftime('%Y-%m-%d %H:%M %Z')}\n"
                f"  Contact window  : {window_label}\n"
                f"  In window       : {'✅ YES' if in_window else '🕐 NO'}\n"
                f"  Force mode      : {'⚡ ON' if force else 'off'}\n"
                f"{'═'*62}"
            )
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("  🧪 DRY-RUN — no messages will be sent\n"))

        # If outside contact window AND not forced, only process urgent leads
        if not in_window and not force:
            self.stdout.write(
                self.style.WARNING(
                    f"  Outside contact windows. "
                    f"Processing URGENT leads only (< {URGENCY_WINDOW_HOURS}h WA window). "
                    f"Next window: {_next_window_opens_in(now_local)}"
                )
            )

        leads = _get_eligible_leads()
        if limit > 0:
            leads = leads[:limit]

        total = leads.count()
        self.stdout.write(f"\n  Eligible leads in WA window : {total}\n")

        stats = dict(
            sent=0, skipped=0, deferred=0,
            window_closed=0, maxed=0, errors=0,
            ai=0, template=0, urgent_override=0,
        )

        for lead in leads:
            try:
                result = self._process_lead(
                    lead, dry_run, force, in_window, window_label, stage_filter
                )
                status = result.get("status", "skipped")
                stats[status] = stats.get(status, 0) + 1
                if result.get("ai"):
                    stats["ai"] += 1
                elif result.get("status") == "sent":
                    stats["template"] += 1
                if result.get("urgent_override"):
                    stats["urgent_override"] += 1
            except Exception as exc:
                logger.exception("Error processing lead %s", lead.id)
                stats["errors"] += 1
                self.stdout.write(self.style.ERROR(f"  ❌ Lead {lead.id}: {exc}"))

        # ── Summary ───────────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS(f"\n{'─'*62}"))
        self.stdout.write(self.style.SUCCESS("  SUMMARY"))
        rows = [
            ("sent",             "✅ Sent"),
            ("ai",               "   └─ AI-generated"),
            ("template",         "   └─ Template fallback"),
            ("urgent_override",  "⚡ Urgent override (outside window)"),
            ("deferred",         "🕐 Deferred (outside window, not urgent)"),
            ("skipped",          "⏭  Skipped (not due yet)"),
            ("maxed",            "🔒 Max attempts reached today"),
            ("window_closed",    "🚫 WA window closed at runtime"),
            ("errors",           "❌ Errors"),
        ]
        for key, label in rows:
            val = stats.get(key, 0)
            if val > 0 or key in ("sent", "deferred", "skipped"):
                self.stdout.write(f"    {label:<42} {val}")
        self.stdout.write("")

    # ── Per-lead processing ───────────────────────────────────────────────────

    def _process_lead(
        self,
        lead: Appointment,
        dry_run: bool,
        force: bool,
        in_window: bool,
        window_label: str,
        stage_filter: str,
    ) -> dict:

        # 1. Runtime window guard
        if not is_window_open(lead):
            self.stdout.write(
                self.style.WARNING(
                    f"  ⏰ WA window closed at runtime for lead {lead.id} — skipping"
                )
            )
            return {"status": "window_closed", "ai": False}

        # 2. Stage filter (testing only)
        stage = _detect_stage(lead)
        if stage_filter and stage != stage_filter:
            return {"status": "skipped", "ai": False}

        # 3. Daily attempt budget
        count_today = _get_todays_attempt_count(lead)
        if count_today >= MAX_DAILY_ATTEMPTS:
            self.stdout.write(
                f"  🔒 Lead {lead.id} — maxed ({count_today}/{MAX_DAILY_ATTEMPTS} today)"
            )
            return {"status": "maxed", "ai": False}

        # 4. Is the next attempt's silence threshold met?
        if not _attempt_is_due(lead, count_today):
            logger.debug(
                "Lead %s not due: silent=%.1fh, threshold=%.1fh",
                lead.id,
                _hours_silent(lead),
                ATTEMPT_THRESHOLDS_HOURS[count_today] if count_today < len(ATTEMPT_THRESHOLDS_HOURS) else 999,
            )
            return {"status": "skipped", "ai": False}

        # 5. Contact-window gate
        #    Pass through if: force mode, currently in a window, OR urgent override
        urgent        = _is_urgent(lead)
        bypass_window = force or in_window or urgent

        if not bypass_window:
            hrs_left = hours_remaining(lead)
            self.stdout.write(
                f"  🕐 Lead {lead.id} — outside window, not urgent "
                f"({hrs_left:.1f}h WA remaining) — deferred"
            )
            return {"status": "deferred", "ai": False}

        # 6. Generate message
        attempt_number = count_today + 1
        result = _generate(lead, stage, attempt_number, window_label, urgent)
        message = result["message"]

        hrs_left  = hours_remaining(lead)
        silent_h  = _hours_silent(lead)
        phone     = (
            lead.phone_number
            .replace("whatsapp:+", "")
            .replace("whatsapp:", "")
            .replace("+", "")
            .strip()
        )
        tag       = "🤖 AI" if result["ai"] else "📄 Tmpl"
        u_tag     = " ⚡URGENT" if urgent else ""
        win_tag   = f"[{window_label}]" if in_window else "[URGENT override]"

        # 7. Dry-run output
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  [DRY-RUN]{u_tag} Lead {lead.id} | stage={stage} | "
                    f"attempt={attempt_number} | {win_tag} | {tag}\n"
                    f"  silent={silent_h:.1f}h | window_left={hrs_left:.1f}h | +{phone}\n"
                    f"  ┌─ {message[:160]}{'…' if len(message) > 160 else ''}"
                )
            )
            return {"status": "sent", "ai": result["ai"], "urgent_override": urgent}

        # 8. Send
        try:
            whatsapp_api.send_text_message(phone, message)
        except Exception as exc:
            logger.exception("WhatsApp send failed for lead %s", lead.id)
            self.stdout.write(self.style.ERROR(f"  ❌ Send failed lead {lead.id}: {exc}"))
            return {"status": "errors", "ai": False}

        # 9. Persist
        sent_at = timezone.now()
        lead.add_conversation_message(
            "assistant",
            f"[24HR FOLLOW-UP #{attempt_number} | {window_label}] {message}",
        )
        lead.last_followup_sent  = sent_at
        lead.last_outbound_at    = sent_at
        lead.last_contacted_at   = sent_at
        lead.save(update_fields=[
            "last_followup_sent", "last_outbound_at", "last_contacted_at",
        ])
        _increment_attempt(lead)

        self.stdout.write(
            self.style.SUCCESS(
                f"  ✅ {tag}{u_tag} Lead {lead.id} | stage={stage} | "
                f"attempt={attempt_number}/{MAX_DAILY_ATTEMPTS} | {win_tag} | "
                f"silent={silent_h:.1f}h | window={hrs_left:.1f}h left | +{phone}"
            )
        )

        return {"status": "sent", "ai": result["ai"], "urgent_override": urgent}