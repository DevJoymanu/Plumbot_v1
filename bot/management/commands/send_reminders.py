# bot/services/whatsapp_messages.py

"""
Django Management Command: send_reminders
=========================================
Run every 15 minutes via Railway Scheduler or cron:

    python manage.py send_reminders

Cron:
    */15 * * * * cd /app && python manage.py send_reminders >> /var/log/reminders.log 2>&1
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from bot.models import Appointment
from bot.whatsapp_cloud_api import whatsapp_api
from openai import OpenAI
import os
import logging
import json

from typing import List

# ============================================================
# BASE CONFIG
# ============================================================

BASE_APPOINTMENT_URL = "https://plumbotv1-production.up.railway.app/appointments"
DEFAULT_CONTACT_NUMBER = "+263610318200"


# ============================================================
# GENERIC HELPERS (NO REPEAT LOGIC)
# ============================================================

def _appointment_url(apt) -> str:
    return f"{BASE_APPOINTMENT_URL}/{apt.id}/"


def _safe(value, fallback="") -> str:
    return value or fallback


def _service(apt) -> str:
    return _safe(getattr(apt, "service_type", ""), "General Plumbing")


def _area(apt) -> str:
    return _safe(getattr(apt, "area", ""), "Location TBC")


def _apt_date(apt) -> str:
    return apt.appointment_date.strftime("%A %d %b %Y")


def _apt_date_short(apt) -> str:
    return apt.appointment_date.strftime("%a %d %b")


def _apt_time(apt) -> str:
    return apt.appointment_time.strftime("%I:%M %p")


def _fmt_phone(phone: str) -> str:
    if not phone:
        return ""
    phone = phone.strip()
    if phone.startswith("+"):
        return phone
    if phone.startswith("0"):
        return "+27" + phone[1:]
    return phone


# ============================================================
# CORE MESSAGE ENGINE (DRY FOUNDATION)
# ============================================================

def _build_message(
    greeting: str,
    header_lines: List[str],
    detail_lines: List[str],
    body_lines: List[str],
    footer_lines: List[str]
) -> str:
    blocks = []

    if greeting:
        blocks.append(greeting)

    if header_lines:
        blocks.append("\n".join(header_lines))

    if detail_lines:
        blocks.append("\n".join(detail_lines))

    if body_lines:
        blocks.append("\n".join(body_lines))

    if footer_lines:
        blocks.append("\n".join(footer_lines))

    return "\n\n".join(blocks)


def _appointment_details_block(apt, include_date=True) -> List[str]:
    details = [
        f"🛠 {_service(apt)}",
        f"📍 {_area(apt)}",
        f"⏰ {_apt_time(apt)}",
    ]

    if include_date:
        details.insert(1, f"📅 {_apt_date(apt)}")

    return details


def _appointment_link_block(apt) -> List[str]:
    return [
        "🔗 View full appointment:",
        _appointment_url(apt)
    ]


# ============================================================
# LEAD REMINDERS (ZERO DUPLICATION)
# ============================================================

def _lead_reminder(
    apt,
    plumber_phone: str,
    greeting_text: str,
    body_lines: List[str]
) -> str:

    name = _safe(getattr(apt, "customer_name", ""), "there")
    contact = plumber_phone or DEFAULT_CONTACT_NUMBER

    return _build_message(
        greeting=greeting_text.format(name=name),
        header_lines=[],
        detail_lines=_appointment_details_block(apt),
        body_lines=body_lines + _appointment_link_block(apt),
        footer_lines=[f"📞 {contact}"]
    )


def msg_lead_2days(apt, plumber_phone: str = "") -> str:
    return _lead_reminder(
        apt,
        plumber_phone,
        greeting_text="Hi {name} 👋",
        body_lines=[
            "Just a friendly reminder about your upcoming appointment.",
            "Please make sure someone is home and the work area is accessible.",
            "We look forward to assisting you! 🔧"
        ]
    )


def msg_lead_1day(apt, plumber_phone: str = "") -> str:
    return _lead_reminder(
        apt,
        plumber_phone,
        greeting_text="Hi {name} 👋",
        body_lines=[
            "Your appointment is *tomorrow!*",
            "See you tomorrow! 🔧"
        ]
    )


def msg_lead_morning(apt, plumber_phone: str = "") -> str:
    return _lead_reminder(
        apt,
        plumber_phone,
        greeting_text="Good morning {name} ☀️",
        body_lines=[
            "Today is your appointment day!",
            "Please ensure someone is available.",
            "See you shortly! 🔧"
        ]
    )


def msg_lead_2hours(apt, plumber_phone: str = "") -> str:
    return _lead_reminder(
        apt,
        plumber_phone,
        greeting_text="Hi {name} ⏰",
        body_lines=[
            "Your plumber will be arriving in approximately *2 hours.*",
            "See you soon! 🔧"
        ]
    )


# ============================================================
# PLUMBER MESSAGE BLOCK BUILDER (DRY)
# ============================================================

def _plumber_appointment_block(index: int, apt, show_date=True) -> str:
    phone = _fmt_phone(getattr(apt, "phone_number", "") or "")
    name = _safe(getattr(apt, "customer_name", "?"), "?")

    date_part = f"{_apt_date_short(apt)} | " if show_date else ""

    lines = [
        f"{index}\ufe0f\u20e3 {name}",
        f"🛠 {_service(apt)}",
        f"📍 {_area(apt)}",
        f"⏰ {date_part}{_apt_time(apt)}",
        f"📞 {phone}",
        f"🔗 {_appointment_url(apt)}"
    ]

    return "\n".join(lines)


def _plumber_summary(title: str, plumber_name: str, appointments, show_date=True) -> str:
    blocks = [
        title,
        f"Hi {plumber_name} 👋"
    ]

    if not appointments:
        blocks.append("No scheduled jobs.")
    else:
        for i, apt in enumerate(appointments, 1):
            blocks.append(_plumber_appointment_block(i, apt, show_date))

    return "\n\n".join(blocks)


# ============================================================
# PLUMBER NOTIFICATIONS
# ============================================================

def msg_plumber_weekly(plumber_name: str, appointments) -> str:
    return _plumber_summary(
        title="📅 *Your Jobs for This Week*",
        plumber_name=plumber_name,
        appointments=appointments,
        show_date=True
    )


def msg_plumber_tomorrow(plumber_name: str, appointments) -> str:
    return _plumber_summary(
        title="📆 *Tomorrow’s Jobs*",
        plumber_name=plumber_name,
        appointments=appointments,
        show_date=False
    )


def msg_plumber_morning(plumber_name: str, appointments) -> str:
    return _plumber_summary(
        title="🌅 *Today’s Jobs*",
        plumber_name=plumber_name,
        appointments=appointments,
        show_date=False
    )


def msg_plumber_2hours(apt, plumber_name: str = "there") -> str:
    phone = _fmt_phone(getattr(apt, "phone_number", "") or "")
    name = _safe(getattr(apt, "customer_name", "?"), "?")

    return _build_message(
        greeting="⏰ *UPCOMING JOB – 2 HOURS*",
        header_lines=[f"Hi {plumber_name} 👋"],
        detail_lines=[
            f"Customer: {name}",
            f"🛠 {_service(apt)}",
            f"📍 {_area(apt)}",
            f"⏰ {_apt_time(apt)}",
            f"📞 {phone}",
            f"🔗 {_appointment_url(apt)}"
        ],
        body_lines=["Make sure you're on your way."],
        footer_lines=["Drive safe! 🚗🔧"]
    )


from typing import List

# ============================================================
# BASE CONFIG
# ============================================================

BASE_APPOINTMENT_URL = "https://plumbotv1-production.up.railway.app/appointments"
DEFAULT_CONTACT_NUMBER = "+263610318200"


# ============================================================
# GENERIC HELPERS (NO REPEAT LOGIC)
# ============================================================

def _appointment_url(apt) -> str:
    return f"{BASE_APPOINTMENT_URL}/{apt.id}/"


def _safe(value, fallback="") -> str:
    return value or fallback


def _service(apt) -> str:
    return _safe(getattr(apt, "service_type", ""), "General Plumbing")


def _area(apt) -> str:
    return _safe(getattr(apt, "area", ""), "Location TBC")


def _apt_date(apt) -> str:
    return apt.appointment_date.strftime("%A %d %b %Y")


def _apt_date_short(apt) -> str:
    return apt.appointment_date.strftime("%a %d %b")


def _apt_time(apt) -> str:
    return apt.appointment_time.strftime("%I:%M %p")


def _fmt_phone(phone: str) -> str:
    if not phone:
        return ""
    phone = phone.strip()
    if phone.startswith("+"):
        return phone
    if phone.startswith("0"):
        return "+27" + phone[1:]
    return phone


# ============================================================
# CORE MESSAGE ENGINE (DRY FOUNDATION)
# ============================================================

def _build_message(
    greeting: str,
    header_lines: List[str],
    detail_lines: List[str],
    body_lines: List[str],
    footer_lines: List[str]
) -> str:
    blocks = []

    if greeting:
        blocks.append(greeting)

    if header_lines:
        blocks.append("\n".join(header_lines))

    if detail_lines:
        blocks.append("\n".join(detail_lines))

    if body_lines:
        blocks.append("\n".join(body_lines))

    if footer_lines:
        blocks.append("\n".join(footer_lines))

    return "\n\n".join(blocks)


def _appointment_details_block(apt, include_date=True) -> List[str]:
    details = [
        f"🛠 {_service(apt)}",
        f"📍 {_area(apt)}",
        f"⏰ {_apt_time(apt)}",
    ]

    if include_date:
        details.insert(1, f"📅 {_apt_date(apt)}")

    return details


def _appointment_link_block(apt) -> List[str]:
    return [
        "🔗 View full appointment:",
        _appointment_url(apt)
    ]


# ============================================================
# LEAD REMINDERS (ZERO DUPLICATION)
# ============================================================

def _lead_reminder(
    apt,
    plumber_phone: str,
    greeting_text: str,
    body_lines: List[str]
) -> str:

    name = _safe(getattr(apt, "customer_name", ""), "there")
    contact = plumber_phone or DEFAULT_CONTACT_NUMBER

    return _build_message(
        greeting=greeting_text.format(name=name),
        header_lines=[],
        detail_lines=_appointment_details_block(apt),
        body_lines=body_lines + _appointment_link_block(apt),
        footer_lines=[f"📞 {contact}"]
    )


def msg_lead_2days(apt, plumber_phone: str = "") -> str:
    return _lead_reminder(
        apt,
        plumber_phone,
        greeting_text="Hi {name} 👋",
        body_lines=[
            "Just a friendly reminder about your upcoming appointment.",
            "Please make sure someone is home and the work area is accessible.",
            "We look forward to assisting you! 🔧"
        ]
    )


def msg_lead_1day(apt, plumber_phone: str = "") -> str:
    return _lead_reminder(
        apt,
        plumber_phone,
        greeting_text="Hi {name} 👋",
        body_lines=[
            "Your appointment is *tomorrow!*",
            "See you tomorrow! 🔧"
        ]
    )


def msg_lead_morning(apt, plumber_phone: str = "") -> str:
    return _lead_reminder(
        apt,
        plumber_phone,
        greeting_text="Good morning {name} ☀️",
        body_lines=[
            "Today is your appointment day!",
            "Please ensure someone is available.",
            "See you shortly! 🔧"
        ]
    )


def msg_lead_2hours(apt, plumber_phone: str = "") -> str:
    return _lead_reminder(
        apt,
        plumber_phone,
        greeting_text="Hi {name} ⏰",
        body_lines=[
            "Your plumber will be arriving in approximately *2 hours.*",
            "See you soon! 🔧"
        ]
    )


# ============================================================
# PLUMBER MESSAGE BLOCK BUILDER (DRY)
# ============================================================

def _plumber_appointment_block(index: int, apt, show_date=True) -> str:
    phone = _fmt_phone(getattr(apt, "phone_number", "") or "")
    name = _safe(getattr(apt, "customer_name", "?"), "?")

    date_part = f"{_apt_date_short(apt)} | " if show_date else ""

    lines = [
        f"{index}\ufe0f\u20e3 {name}",
        f"🛠 {_service(apt)}",
        f"📍 {_area(apt)}",
        f"⏰ {date_part}{_apt_time(apt)}",
        f"📞 {phone}",
        f"🔗 {_appointment_url(apt)}"
    ]

    return "\n".join(lines)


def _plumber_summary(title: str, plumber_name: str, appointments, show_date=True) -> str:
    blocks = [
        title,
        f"Hi {plumber_name} 👋"
    ]

    if not appointments:
        blocks.append("No scheduled jobs.")
    else:
        for i, apt in enumerate(appointments, 1):
            blocks.append(_plumber_appointment_block(i, apt, show_date))

    return "\n\n".join(blocks)


# ============================================================
# PLUMBER NOTIFICATIONS
# ============================================================

def msg_plumber_weekly(plumber_name: str, appointments) -> str:
    return _plumber_summary(
        title="📅 *Your Jobs for This Week*",
        plumber_name=plumber_name,
        appointments=appointments,
        show_date=True
    )


def msg_plumber_tomorrow(plumber_name: str, appointments) -> str:
    return _plumber_summary(
        title="📆 *Tomorrow’s Jobs*",
        plumber_name=plumber_name,
        appointments=appointments,
        show_date=False
    )


def msg_plumber_morning(plumber_name: str, appointments) -> str:
    return _plumber_summary(
        title="🌅 *Today’s Jobs*",
        plumber_name=plumber_name,
        appointments=appointments,
        show_date=False
    )


def msg_plumber_2hours(apt, plumber_name: str = "there") -> str:
    phone = _fmt_phone(getattr(apt, "phone_number", "") or "")
    name = _safe(getattr(apt, "customer_name", "?"), "?")

    return _build_message(
        greeting="⏰ *UPCOMING JOB – 2 HOURS*",
        header_lines=[f"Hi {plumber_name} 👋"],
        detail_lines=[
            f"Customer: {name}",
            f"🛠 {_service(apt)}",
            f"📍 {_area(apt)}",
            f"⏰ {_apt_time(apt)}",
            f"📞 {phone}",
            f"🔗 {_appointment_url(apt)}"
        ],
        body_lines=["Make sure you're on your way."],
        footer_lines=["Drive safe! 🚗🔧"]
    )
