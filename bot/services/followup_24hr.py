"""
bot/services/followup_24hr.py
==============================
Reusable service layer for the 24-hour window follow-up system.
Contact-window aware — messages only go out during appropriate hours,
with an urgency override when the WhatsApp window is about to close.

Public API
----------
  detect_stage(appointment)      → str   conversation stage
  should_send_followup(appt)     → bool  is a follow-up due right now?
  trigger_followup(appointment)  → dict  send if due, return result
  get_follow_up_status(appt)     → dict  dashboard-friendly state summary

Contact Windows (SAST)
-----------------------
  08:00–10:00  Morning
  12:00–14:00  Lunch
  17:00–20:00  After-work

Urgency Override
-----------------
When < URGENCY_WINDOW_HOURS remain in the WhatsApp 24-hour window the
contact-window gate is bypassed so leads are never lost due to timing gaps.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import timedelta
from typing import Optional

import pytz
from django.utils import timezone
from openai import OpenAI

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

SA_TZ = pytz.timezone("Africa/Johannesburg")

CONTACT_WINDOWS: list[tuple[int, int]] = [
    (8,  10),   # Morning
    (12, 14),   # Lunch
    (17, 20),   # After-work / evening
]

ATTEMPT_THRESHOLDS_HOURS: list[float] = [2.0, 5.0, 10.0]
MAX_DAILY_ATTEMPTS: int = 3
MIN_SILENCE_MINUTES: int = 90
URGENCY_WINDOW_HOURS: float = 3.0

_DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")
_deepseek = (
    OpenAI(api_key=_DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")
    if _DEEPSEEK_KEY
    else None
)


# ══════════════════════════════════════════════════════════════════════════════
# CONTACT WINDOW HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _now_sast():
    return timezone.now().astimezone(SA_TZ)


def in_contact_window(now_local=None) -> bool:
    """True if the current SAST hour is inside any contact window."""
    hour = (now_local or _now_sast()).hour
    return any(s <= hour < e for s, e in CONTACT_WINDOWS)


def current_window_label(now_local=None) -> str:
    hour = (now_local or _now_sast()).hour
    names = {8: "Morning", 12: "Lunch", 17: "After-work"}
    for start, end in CONTACT_WINDOWS:
        if start <= hour < end:
            return names.get(start, f"{start:02d}:00–{end:02d}:00")
    return "Outside window"


def next_window_in_hours(now_local=None) -> float:
    """Hours until the next contact window opens."""
    local = now_local or _now_sast()
    hour = local.hour
    for start, _ in CONTACT_WINDOWS:
        if hour < start:
            return float(start - hour)
    return float(24 - hour + CONTACT_WINDOWS[0][0])


def is_urgent(appointment) -> bool:
    """True when the WA window will close within URGENCY_WINDOW_HOURS."""
    from bot.whatsapp_window import hours_remaining
    return hours_remaining(appointment) <= URGENCY_WINDOW_HOURS


# ══════════════════════════════════════════════════════════════════════════════
# ATTEMPT TRACKING
# ══════════════════════════════════════════════════════════════════════════════

def _today_key() -> str:
    return timezone.now().astimezone(SA_TZ).strftime("%Y-%m-%d")


def get_today_attempt_count(appointment) -> int:
    today = _today_key()
    notes = appointment.internal_notes or ""
    match = re.search(rf"\[24HR_FU:{re.escape(today)}\]:(\d+)", notes)
    return int(match.group(1)) if match else 0


def _increment_attempt(appointment) -> int:
    today = _today_key()
    notes = appointment.internal_notes or ""
    pattern = rf"\[24HR_FU:{re.escape(today)}\]:(\d+)"
    match = re.search(pattern, notes)
    if match:
        new_count = int(match.group(1)) + 1
        new_notes = re.sub(pattern, f"[24HR_FU:{today}]:{new_count}", notes)
    else:
        ts = _now_sast().strftime("%H:%M")
        new_count = 1
        new_notes = f"{notes}\n[24HR_FU:{today}]:1 first={ts}".strip()
    appointment.internal_notes = new_notes
    appointment.save(update_fields=["internal_notes"])
    return new_count


# ══════════════════════════════════════════════════════════════════════════════
# STAGE DETECTION
# ══════════════════════════════════════════════════════════════════════════════

STAGES = [
    "quote_sent", "images_viewed", "appointment_pending", "site_visit_booked",
    "plan_requested", "pricing_asked", "intake_stalled", "generic",
]


def detect_stage(appointment) -> str:
    """Return the conversation stage string for this lead."""
    history = appointment.conversation_history or []

    try:
        if appointment.quotations.filter(status="sent").exists():
            return "quote_sent"
    except Exception:
        pass

    photos_at = getattr(appointment, "previous_work_photos_sent_at", None)
    if photos_at:
        last_in = appointment.last_customer_response
        if not last_in or last_in < photos_at:
            return "images_viewed"

    if appointment.status == "confirmed" and appointment.scheduled_datetime:
        return "site_visit_booked"

    if (
        appointment.project_type
        and appointment.customer_area
        and appointment.scheduled_datetime
        and appointment.status != "confirmed"
    ):
        return "appointment_pending"

    if (
        appointment.has_plan is True
        and not appointment.plan_file
        and appointment.plan_status in ("pending_upload", None, "")
    ):
        return "plan_requested"

    sent_pricing = bool(getattr(appointment, "sent_pricing_intents", None)) or bool(
        getattr(appointment, "pricing_overview_sent", False)
    )
    if sent_pricing and not appointment.scheduled_datetime:
        return "pricing_asked"

    skip = ("[AUTO", "[MANUAL", "[BULK", "[24HR", "[Sent ", "[FILE", "[VIDEO")
    has_bot_msg = any(
        m.get("role") == "assistant"
        and not any((m.get("content") or "").startswith(p) for p in skip)
        for m in history
    )
    if has_bot_msg:
        return "intake_stalled"

    return "generic"


# ══════════════════════════════════════════════════════════════════════════════
# ELIGIBILITY CHECK
# ══════════════════════════════════════════════════════════════════════════════

def hours_since_response(appointment) -> float:
    ref = appointment.last_customer_response or appointment.created_at
    return (timezone.now() - ref).total_seconds() / 3600


def should_send_followup(appointment, force: bool = False) -> tuple[bool, str]:
    """
    Return (eligible, reason_string).

    `force=True` skips the contact-window gate (use for manual triggers /
    management command --force flag).

    Urgency override is always applied regardless of force — if the window
    is closing in < URGENCY_WINDOW_HOURS the gate is bypassed automatically.
    """
    from bot.whatsapp_window import is_window_open

    if not is_window_open(appointment):
        return False, "wa_window_closed"

    if getattr(appointment, "chatbot_paused", False):
        return False, "chatbot_paused"

    silence_h = hours_since_response(appointment)
    if silence_h < (MIN_SILENCE_MINUTES / 60):
        return False, f"too_soon ({silence_h:.1f}h < {MIN_SILENCE_MINUTES/60:.1f}h min)"

    count_today = get_today_attempt_count(appointment)
    if count_today >= MAX_DAILY_ATTEMPTS:
        return False, f"max_attempts ({count_today}/{MAX_DAILY_ATTEMPTS})"

    next_idx = count_today
    if next_idx >= len(ATTEMPT_THRESHOLDS_HOURS):
        return False, "all_thresholds_passed"

    required_silence = ATTEMPT_THRESHOLDS_HOURS[next_idx]
    if silence_h < required_silence:
        return False, f"not_due ({silence_h:.1f}h < {required_silence:.1f}h threshold)"

    # Contact-window gate
    now_local = _now_sast()
    _urgent   = is_urgent(appointment)
    if not force and not in_contact_window(now_local) and not _urgent:
        next_h = next_window_in_hours(now_local)
        return False, f"outside_window (next in ~{next_h:.0f}h)"

    return True, "ok"


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def _service_label(appointment) -> str:
    mapping = {
        "bathroom_renovation":       "bathroom renovation",
        "kitchen_renovation":        "kitchen renovation",
        "new_plumbing_installation": "new plumbing installation",
        "Bathroom Renovation":       "bathroom renovation",
        "Kitchen Renovation":        "kitchen renovation",
        "New Plumbing Installation": "new plumbing installation",
    }
    return mapping.get(appointment.project_type or "", "plumbing project")


def _snippet(appointment, n: int = 6) -> str:
    history = appointment.conversation_history or []
    skip = ("[AUTO", "[MANUAL", "[BULK", "[24HR", "[Sent ", "[FILE", "[VIDEO")
    lines = []
    for msg in history[-n:]:
        c = (msg.get("content") or "").strip()
        if not c or any(c.startswith(p) for p in skip):
            continue
        role = "Customer" if msg.get("role") == "user" else "Bot"
        lines.append(f"{role}: {c[:180]}")
    return "\n".join(lines) if lines else "No prior conversation."


def _last_bot_q(appointment) -> str:
    for msg in reversed(appointment.conversation_history or []):
        if msg.get("role") != "assistant":
            continue
        c = (msg.get("content") or "").strip()
        if c.startswith("["):
            continue
        if "?" in c:
            return c[:300]
    return ""


def _template(appointment, stage: str, attempt: int) -> str:
    service  = _service_label(appointment)
    area     = appointment.customer_area or ""
    name     = appointment.customer_name or ""
    g        = f"Hi {name}," if name else "Hi there,"
    plumber  = (
        getattr(appointment, "plumber_contact_number", None) or "+263774819901"
    ).replace("+", "").replace("whatsapp:", "")

    bank: dict[str, list[str]] = {
        "quote_sent": [
            f"{g} did you get a chance to look over the quotation? Happy to answer any questions.",
            f"{g} did the numbers make sense? Our plumber can walk you through the details.",
            f"Still interested in the {service}? Just say the word.",
        ],
        "images_viewed": [
            f"{g} did any of those photos catch your eye? We can do a similar finish for your {service}.",
            (
                f"{g} those were from a job we finished recently"
                f"{f' in {area}' if area else ''}. "
                f"Want us to come through and show you what's possible?"
            ),
            "Still thinking? No rush — just say go when you're ready.",
        ],
        "appointment_pending": [
            f"{g} you're all set on our side — just need your confirmation to lock in the slot. Shall I confirm?",
            f"{g} the slot won't stay open forever — want me to confirm it now?",
            "Confirm the visit?",
        ],
        "site_visit_booked": [
            f"{g} your site visit is confirmed. Our plumber will call 30 minutes before arrival. Any questions?",
            f"{g} looking forward to the visit! If anything changes, just let us know. Contact: {plumber}",
            f"See you soon! Call us on {plumber}.",
        ],
        "plan_requested": [
            f"{g} whenever you get a chance, send through the plan for your {service} — even a phone photo is fine. 📐",
            f"{g} once we have the plan, we can lock in your appointment straight away.",
            "Send the plan when ready — or we can do a free site visit instead.",
        ],
        "pricing_asked": [
            f"{g} hope the pricing made sense! A free site visit locks in a fixed price on the spot. Want to book one?",
            f"{g} the best way to get an exact figure is a quick on-site look. Free, takes about an hour. What day works?",
            "Free site visit — shall I book one?",
        ],
        "intake_stalled": [
            (
                f"{g} picking up from where we left off — which service were you after? "
                f"Bathroom renovation, kitchen renovation, or new plumbing installation?"
            ),
            f"{g} still happy to help — which service did you need?",
            "Still need help with your plumbing?",
        ],
        "generic": [
            f"{g} still looking to get the {service} sorted? Happy to help.",
            f"{g} happy to help whenever you're ready. Any questions?",
            "Still keen? Just say the word. 😊",
        ],
    }

    options = bank.get(stage, bank["generic"])
    return options[min(attempt - 1, len(options) - 1)]


def _ai_generate(
    appointment,
    stage: str,
    attempt: int,
    tmpl: str,
    window_label: str,
    urgent: bool,
) -> str:
    if not _deepseek:
        return tmpl

    from bot.whatsapp_window import hours_remaining as _hrs_remaining

    silent_h  = hours_since_response(appointment)
    hrs_left  = _hrs_remaining(appointment)
    service   = _service_label(appointment)
    area      = appointment.customer_area or "not yet shared"

    urgency_note = (
        f"⚠️ Only {hrs_left:.1f}h left in the WhatsApp window — final chance. "
        f"Make it count but keep it completely natural."
        if urgent else ""
    )
    length = (
        "1 sentence only — ultra-short, 9-word style. No emoji."
        if attempt >= 3 else "2–3 sentences. At most 1 emoji."
    )

    prompt = f"""You write WhatsApp follow-ups for Homebase Plumbers (Zimbabwe/South Africa).

CONTEXT:
{_snippet(appointment)}

LAST UNANSWERED QUESTION:
{_last_bot_q(appointment) or "None"}

DETAILS:
- Stage: {stage}  |  Service: {service}  |  Area: {area}
- Silent: {silent_h:.1f}h  |  Attempt #{attempt}  |  Window: {window_label}  |  WA left: {hrs_left:.1f}h
{urgency_note}

TEMPLATE (stay close to this):
\"\"\"{tmpl}\"\"\"

RULES:
1. Open with "Hi there," — never use or ask for the customer's name
2. Same intent as template, lightly rephrased
3. One question max  |  {length}
4. SA/ZW English ("sorted", "keen", "sharp")
5. No markdown, no bold, no bullets
6. Never: "just checking in", "following up", "hope you're well", "touching base"
7. Sound like a real person texting

Output ONLY the message text."""

    try:
        resp = _deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Short WhatsApp messages. Sound human. Open with 'Hi there,'."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.42,
            max_tokens=180,
        )
        return resp.choices[0].message.content.strip().replace("**", "").replace("__", "")
    except Exception as exc:
        logger.warning("DeepSeek follow-up failed for %s: %s", appointment.id, exc)
        return tmpl


def generate_message(appointment, stage: str, attempt: int) -> dict:
    """Return {'message': str, 'ai': bool, 'stage': str, 'attempt': int}."""
    now_local    = _now_sast()
    window_label = current_window_label(now_local)
    urgent       = is_urgent(appointment)
    tmpl         = _template(appointment, stage, attempt)

    if _deepseek:
        try:
            ai_msg = _ai_generate(appointment, stage, attempt, tmpl, window_label, urgent)
            return {"message": ai_msg, "ai": True, "stage": stage, "attempt": attempt}
        except Exception as exc:
            logger.warning("AI generation error for %s: %s", appointment.id, exc)

    return {"message": tmpl, "ai": False, "stage": stage, "attempt": attempt}


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD STATUS
# ══════════════════════════════════════════════════════════════════════════════

def get_follow_up_status(appointment) -> dict:
    """
    Return a full status dict suitable for dashboards and admin views.
    """
    from bot.whatsapp_window import is_window_open, hours_remaining as _hrs

    now_local   = _now_sast()
    wa_open     = is_window_open(appointment)
    hrs_left    = _hrs(appointment) if wa_open else 0.0
    count_today = get_today_attempt_count(appointment)
    silence_h   = hours_since_response(appointment)
    stage       = detect_stage(appointment)
    _urgent     = is_urgent(appointment) if wa_open else False
    in_win      = in_contact_window(now_local)
    eligible, reason = should_send_followup(appointment) if wa_open else (False, "wa_window_closed")

    next_attempt = count_today + 1 if count_today < MAX_DAILY_ATTEMPTS else None
    next_threshold = (
        ATTEMPT_THRESHOLDS_HOURS[count_today]
        if count_today < len(ATTEMPT_THRESHOLDS_HOURS)
        else None
    )

    return {
        "lead_id":            appointment.id,
        "stage":              stage,
        "wa_window_open":     wa_open,
        "wa_hours_remaining": round(hrs_left, 2),
        "is_urgent":          _urgent,
        "in_contact_window":  in_win,
        "window_label":       current_window_label(now_local),
        "next_window_in_h":   round(next_window_in_hours(now_local), 1),
        "silence_hours":      round(silence_h, 2),
        "attempts_today":     count_today,
        "max_attempts":       MAX_DAILY_ATTEMPTS,
        "next_attempt_num":   next_attempt,
        "next_threshold_h":   next_threshold,
        "eligible_now":       eligible,
        "ineligible_reason":  reason if not eligible else None,
        "contact_windows":    CONTACT_WINDOWS,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN TRIGGER  (called from webhook / admin / management command)
# ══════════════════════════════════════════════════════════════════════════════

def trigger_followup(appointment, force: bool = False) -> dict:
    """
    Check eligibility and send a follow-up if due.

    Parameters
    ----------
    appointment : Appointment
    force       : bool — skip the contact-window gate (for admin manual sends)

    Returns
    -------
    dict with keys: sent, reason, message, stage, attempt, ai, urgent_override
    """
    from bot.whatsapp_cloud_api import whatsapp_api
    from bot.whatsapp_window import hours_remaining as _hrs

    eligible, reason = should_send_followup(appointment, force=force)
    if not eligible:
        return {
            "sent": False, "reason": reason,
            "message": "", "stage": "", "attempt": 0,
            "ai": False, "urgent_override": False,
        }

    count_today    = get_today_attempt_count(appointment)
    attempt_number = count_today + 1
    stage          = detect_stage(appointment)
    urgent         = is_urgent(appointment)
    result         = generate_message(appointment, stage, attempt_number)
    message        = result["message"]

    clean_phone = (
        appointment.phone_number
        .replace("whatsapp:+", "")
        .replace("whatsapp:", "")
        .replace("+", "")
        .strip()
    )

    try:
        whatsapp_api.send_text_message(clean_phone, message)
    except Exception as exc:
        logger.exception("Failed to send 24hr follow-up for lead %s", appointment.id)
        return {
            "sent": False, "reason": f"send_error: {exc}",
            "message": message, "stage": stage, "attempt": attempt_number,
            "ai": result["ai"], "urgent_override": urgent,
        }

    # Persist
    sent_at      = timezone.now()
    window_label = current_window_label()
    appointment.add_conversation_message(
        "assistant",
        f"[24HR FOLLOW-UP #{attempt_number} | {window_label}{'| URGENT' if urgent else ''}] {message}",
    )
    appointment.last_followup_sent = sent_at
    appointment.last_outbound_at   = sent_at
    appointment.last_contacted_at  = sent_at
    appointment.save(update_fields=[
        "last_followup_sent", "last_outbound_at", "last_contacted_at",
    ])
    _increment_attempt(appointment)

    logger.info(
        "24hr follow-up sent | lead=%s stage=%s attempt=%d/%d ai=%s "
        "window=%s urgent=%s wa_left=%.1fh",
        appointment.id, stage, attempt_number, MAX_DAILY_ATTEMPTS,
        result["ai"], window_label, urgent, _hrs(appointment),
    )

    return {
        "sent":           True,
        "reason":         "ok",
        "message":        message,
        "stage":          stage,
        "attempt":        attempt_number,
        "ai":             result["ai"],
        "urgent_override": urgent,
    }