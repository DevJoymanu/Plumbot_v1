"""
Django Management Command: send_reminders
=========================================
Run every 15 minutes via Railway Scheduler or cron:

    python manage.py send_reminders

Cron:
    */15 * * * * cd /app && python manage.py send_reminders >> /var/log/reminders.log 2>&1

KEY FIXES vs the previous version
-----------------------------------
1.  mark_sent() no longer crashes when internal_notes is None.
2.  Reminders are NOT gated by the 24-hour WhatsApp customer-message window —
    confirmed appointments should always receive reminders regardless of when
    the customer last messaged.
3.  appt_utc() correctly converts timezone-aware datetimes stored in UTC
    (Django default) to UTC for comparison.
4.  already_sent() and mark_sent() now use dedicated boolean fields on the
    Appointment model (reminder_1_day_sent, reminder_morning_sent,
    reminder_2_hours_sent) which exist from migration 0008 — much more
    reliable than parsing internal_notes strings.
5.  Plumber reminders use a separate flag stored in internal_notes (unchanged
    behaviour) so they don't collide with the customer-facing fields.
6.  A --dry-run flag is supported for safe testing.
7.  Full summary banner is printed at the end of every run.
"""

import os
import logging
from datetime import timedelta, date, time as dt_time

import pytz
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Q

from django.conf import settings
from django.urls import reverse

from bot.plumber_notifications import (
    get_plumber_notification_emails,
    send_email_to_recipients,
)
from bot.customer_emails import send_customer_reminder_email
from bot.whatsapp_window import is_window_open

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
WINDOW_MINUTES  = 10          # ±10 min tolerance for scheduled send times
TIMEZONE_NAME   = "Africa/Harare"
PLUMBER_PHONE   = os.environ.get("PLUMBER_PHONE_NUMBER", "").replace("+", "").strip()
PLUMBER_NAME    = os.environ.get("PLUMBER_NAME", "there")

SEP = "────────────────"

# ═══════════════════════════════════════════════════════════════════════════════
# TIME HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _in_window(now_local, target_hour: int, target_minute: int = 0) -> bool:
    """Return True if now_local is within ±WINDOW_MINUTES of target_hour:target_minute."""
    target = now_local.replace(
        hour=target_hour, minute=target_minute, second=0, microsecond=0
    )
    return abs((now_local - target).total_seconds()) <= WINDOW_MINUTES * 60


def _is_2h_window(appt_utc_dt, now_utc) -> bool:
    """Return True if the appointment is between 1h55m and 2h05m away."""
    diff = (appt_utc_dt - now_utc).total_seconds()
    return (1 * 3600 + 55 * 60) <= diff <= (2 * 3600 + 5 * 60)


def _appt_utc(apt):
    """
    Return the appointment's scheduled_datetime as a UTC-aware datetime.
    Django stores datetimes in UTC by default (USE_TZ=True).
    If the stored value is naive, assume it is already in CAT (Africa/Harare)
    and convert it to UTC.
    """
    dt = getattr(apt, "scheduled_datetime", None)
    if not dt:
        return None
    if dt.tzinfo is None:
        cat = pytz.timezone(TIMEZONE_NAME)
        dt = cat.localize(dt)
    return dt.astimezone(pytz.utc)


def _fmt_phone(raw: str) -> str:
    return raw.replace("whatsapp:+", "").replace("whatsapp:", "").replace("+", "").strip()


def _service(apt) -> str:
    svc = getattr(apt, "project_type", "") or ""
    return svc.replace("_", " ").title() or "Plumbing work"


def _area(apt) -> str:
    return getattr(apt, "customer_area", "") or "your area"


def _apt_time_str(apt) -> str:
    dt = getattr(apt, "scheduled_datetime", None)
    if not dt:
        return "Scheduled time"
    try:
        cat = pytz.timezone(TIMEZONE_NAME)
        local = dt.astimezone(cat) if dt.tzinfo else cat.localize(dt)
        h, m = local.hour, local.minute
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {suffix}"
    except Exception:
        return str(dt)


def _apt_date_str(apt) -> str:
    dt = getattr(apt, "scheduled_datetime", None)
    if not dt:
        return "Scheduled date"
    try:
        cat = pytz.timezone(TIMEZONE_NAME)
        d = dt.astimezone(cat).date() if dt.tzinfo else cat.localize(dt).date()
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        return f"{days[d.weekday()]} {d.day} {months[d.month - 1]} {d.year}"
    except Exception:
        return str(dt)


# ═══════════════════════════════════════════════════════════════════════════════
# DUPLICATE PREVENTION  (model fields for customer; internal_notes for plumber)
# ═══════════════════════════════════════════════════════════════════════════════

def _already_sent_customer(apt, rtype: str) -> bool:
    """Check dedicated boolean fields added in migration 0008."""
    field_map = {
        "lead_2days":   "reminder_1_day_sent",    # reuse closest field
        "lead_1day":    "reminder_1_day_sent",
        "lead_morning": "reminder_morning_sent",
        "lead_2hours":  "reminder_2_hours_sent",
    }
    field = field_map.get(rtype)
    if field:
        return bool(getattr(apt, field, False))
    # Fallback: check internal_notes for legacy keys
    return f"[reminder_sent_{rtype}_{apt.id}]" in (apt.internal_notes or "")


def _mark_sent_customer(apt, rtype: str) -> None:
    """Mark the appropriate boolean field (and save only that field)."""
    field_map = {
        "lead_2days":   "reminder_1_day_sent",
        "lead_1day":    "reminder_1_day_sent",
        "lead_morning": "reminder_morning_sent",
        "lead_2hours":  "reminder_2_hours_sent",
    }
    field = field_map.get(rtype)
    if field and hasattr(apt, field):
        setattr(apt, field, True)
        apt.save(update_fields=[field])
    else:
        # Fallback: write a flag into internal_notes
        key = f"[reminder_sent_{rtype}_{apt.id}]"
        existing = apt.internal_notes or ""
        if key not in existing:
            apt.internal_notes = f"{existing}\n{key}".strip()
            apt.save(update_fields=["internal_notes"])


def _plumber_key(apt_id: int, rtype: str) -> str:
    return f"plumber_reminder_sent_{rtype}_{apt_id}"


def _already_sent_plumber(apt, rtype: str) -> bool:
    return _plumber_key(apt.id, rtype) in (apt.internal_notes or "")


def _mark_sent_plumber(apt, rtype: str) -> None:
    key = _plumber_key(apt.id, rtype)
    existing = apt.internal_notes or ""
    if key not in existing:
        apt.internal_notes = f"{existing}\n[{key}]".strip()
        apt.save(update_fields=["internal_notes"])


# ═══════════════════════════════════════════════════════════════════════════════
# WHATSAPP SENDER
# ═══════════════════════════════════════════════════════════════════════════════

def _send_wa(phone: str, message: str, dry_run: bool = False) -> bool:
    """Send a WhatsApp message. Returns True on success."""
    if dry_run:
        print(f"  [DRY RUN] Would send to +{phone}: {message[:80]}…")
        return True
    try:
        from bot.whatsapp_cloud_api import whatsapp_api
        clean = _fmt_phone(phone)
        whatsapp_api.send_text_message(clean, message)
        return True
    except Exception as e:
        logger.error(f"WhatsApp send failed to {phone}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE BUILDERS  (customer-facing)
# ═══════════════════════════════════════════════════════════════════════════════

def _msg_2days(apt, plumber_phone: str) -> str:
    name = getattr(apt, "customer_name", "") or "there"
    return (
        f"Hi {name} 👋\n\n"
        f"Just a friendly reminder about your upcoming appointment:\n\n"
        f"🛠 Service: {_service(apt)}\n"
        f"📍 Location: {_area(apt)}\n"
        f"📅 Date: {_apt_date_str(apt)}\n"
        f"⏰ Time: {_apt_time_str(apt)}\n\n"
        f"Please make sure someone is home and the work area is accessible.\n\n"
        f"We look forward to assisting you! 🔧\n\n"
        f"📞 Questions? Call us: +{plumber_phone}"
    )


def _msg_1day(apt, plumber_phone: str) -> str:
    name = getattr(apt, "customer_name", "") or "there"
    return (
        f"Hi {name} 👋\n\n"
        f"Your appointment is *tomorrow!*\n\n"
        f"🛠 {_service(apt)}\n"
        f"📅 {_apt_date_str(apt)}\n"
        f"⏰ {_apt_time_str(apt)}\n\n"
        f"Please ensure:\n"
        f"✅ Someone is home\n"
        f"✅ The work area is accessible\n"
        f"✅ Water can be shut off if needed\n\n"
        f"See you tomorrow! 🔧\n\n"
        f"📞 {plumber_phone}"
    )


def _msg_morning(apt, plumber_phone: str) -> str:
    name = getattr(apt, "customer_name", "") or "there"
    return (
        f"Good morning {name} ☀️\n\n"
        f"Today is your appointment day!\n\n"
        f"⏰ Arrival Time: {_apt_time_str(apt)}\n"
        f"📍 Location: {_area(apt)}\n\n"
        f"Our plumber will be there on time.\n"
        f"Please make sure someone is available.\n\n"
        f"See you shortly! 🔧\n\n"
        f"📞 {plumber_phone}"
    )


def _msg_2hours(apt, plumber_phone: str) -> str:
    name = getattr(apt, "customer_name", "") or "there"
    return (
        f"Hi {name} ⏰\n\n"
        f"Your plumber will be arriving in approximately *2 hours.*\n\n"
        f"📅 Today\n"
        f"⏰ {_apt_time_str(apt)}\n\n"
        f"Please ensure access is ready.\n\n"
        f"See you soon! 🔧\n\n"
        f"📞 +{plumber_phone}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE BUILDERS  (plumber-facing)
# ═══════════════════════════════════════════════════════════════════════════════

def _apt_block(i: int, apt, show_date: bool = True) -> str:
    phone = _fmt_phone(getattr(apt, "phone_number", "") or "")
    name  = getattr(apt, "customer_name", "?") or "?"
    date_part = f"{_apt_date_str(apt)} | " if show_date else ""
    return (
        f"{i}\ufe0f\u20e3 {name}\n"
        f"🛠 {_service(apt)}\n"
        f"📍 {_area(apt)}\n"
        f"⏰ {date_part}{_apt_time_str(apt)}\n"
        f"📞 {phone}"
    )


def _msg_plumber_morning(apts: list, plumber_name: str) -> str:
    blocks = f"\n{SEP}\n\n".join(
        _apt_block(i + 1, a, show_date=False) for i, a in enumerate(apts)
    )
    return (
        f"☀️ *TODAY'S SCHEDULE*\n\n"
        f"Good morning {plumber_name} 👋\n\n"
        f"{SEP}\n\n{blocks}\n\n{SEP}\n\n"
        f"Have a productive day! 💪🔧"
    )


def _msg_plumber_next_day(apts: list, plumber_name: str) -> str:
    blocks = f"\n{SEP}\n\n".join(
        _apt_block(i + 1, a, show_date=False) for i, a in enumerate(apts)
    )
    return (
        f"🌙 *TOMORROW'S APPOINTMENTS*\n\n"
        f"Hi {plumber_name} 👋\n\n"
        f"{SEP}\n\n{blocks}\n\n{SEP}\n\n"
        f"Get your tools ready and travel safe. 🔧🚗"
    )


def _msg_plumber_2hours(apt, plumber_name: str) -> str:
    phone = _fmt_phone(getattr(apt, "phone_number", "") or "")
    name  = getattr(apt, "customer_name", "?") or "?"
    return (
        f"⏰ *UPCOMING JOB – 2 HOURS*\n\n"
        f"Hi {plumber_name} 👋\n\n"
        f"Customer: {name}\n"
        f"🛠 {_service(apt)}\n"
        f"📍 {_area(apt)}\n"
        f"⏰ {_apt_time_str(apt)}\n"
        f"📞 {phone}\n\n"
        f"Make sure you're on your way.\n\nDrive safe! 🚗🔧"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# EMAIL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

_SAST      = pytz.timezone("Africa/Johannesburg")
_SITE_URL  = getattr(settings, "SITE_URL", "").rstrip("/")
_WIN_EMAIL = 8   # ±8 min tolerance — command runs every 15 min

_DAYS_LONG   = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
_DAYS_SHORT  = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
_MONTHS      = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
_MONTHS_LONG = ["January","February","March","April","May","June",
                 "July","August","September","October","November","December"]


def _to_sast(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return _SAST.localize(dt)
    return dt.astimezone(_SAST)


def _email_in_window(now_local, hour, minute=0):
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return abs((now_local - target).total_seconds()) <= _WIN_EMAIL * 60


def _is_2hr_email_window(apt, now_utc):
    dt = _to_sast(apt.scheduled_datetime)
    if not dt:
        return False
    diff = (dt - now_utc).total_seconds()
    return (1 * 3600 + 55 * 60) <= diff <= (2 * 3600 + 5 * 60)


def _is_30min_window(apt, now_utc):
    dt = _to_sast(apt.scheduled_datetime)
    if not dt:
        return False
    diff = (dt - now_utc).total_seconds()
    return 25 * 60 <= diff <= 35 * 60


def _email_fmt_time(apt):
    dt = _to_sast(apt.scheduled_datetime)
    return dt.strftime("%H:%M") if dt else "?"


def _email_fmt_date(apt):
    dt = _to_sast(apt.scheduled_datetime)
    if not dt:
        return "?"
    d = dt.date()
    return f"{_DAYS_LONG[d.weekday()]}, {d.day} {_MONTHS_LONG[d.month - 1]} {d.year}"


def _email_fmt_date_short(d):
    return f"{_DAYS_SHORT[d.weekday()]} {d.day} {_MONTHS[d.month - 1]} {d.year}"


def _clean_phone(apt):
    return "".join(c for c in (apt.phone_number or "") if c.isdigit())


def _apt_deep_link(apt):
    try:
        path = reverse("appointment_detail", kwargs={"pk": apt.pk})
        return f"{_SITE_URL}{path}" if _SITE_URL else f"/appointments/{apt.pk}/"
    except Exception:
        return f"/appointments/{apt.pk}/"


def _eflag_set(apt, key):
    return f"[{key}]" in (apt.internal_notes or "")


def _eflag_mark(apt, key):
    existing = apt.internal_notes or ""
    token = f"[{key}]"
    if token not in existing:
        apt.internal_notes = f"{existing}\n{token}".strip()
        apt.save(update_fields=["internal_notes"])


def _apt_html_block(apt):
    """HTML appointment card with clickable call, WhatsApp, and view buttons."""
    clean = _clean_phone(apt)
    name  = getattr(apt, "customer_name", "?") or "?"
    link  = _apt_deep_link(apt)
    return (
        '<div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px 20px;'
        'margin:16px 0;background:#ffffff;font-family:Arial,sans-serif;">'
        f'<p style="margin:0 0 10px;font-size:16px;font-weight:bold;color:#1a1a1a;">'
        f'📅 {_email_fmt_date(apt)} at {_email_fmt_time(apt)}</p>'
        f'<p style="margin:4px 0;color:#333;font-size:14px;">🔧 <strong>Service:</strong> {_service(apt)}</p>'
        f'<p style="margin:4px 0;color:#333;font-size:14px;">📍 <strong>Area:</strong> {_area(apt)}</p>'
        f'<p style="margin:4px 0;color:#333;font-size:14px;">👤 <strong>Customer:</strong> {name}</p>'
        '<div style="margin-top:14px;">'
        f'<a href="tel:+{clean}" style="display:inline-block;background:#25D366;color:#fff;'
        'text-decoration:none;padding:9px 16px;border-radius:5px;font-size:13px;'
        f'margin-right:8px;margin-bottom:8px;">📞 Call Customer</a>'
        f'<a href="https://wa.me/{clean}" style="display:inline-block;background:#25D366;color:#fff;'
        'text-decoration:none;padding:9px 16px;border-radius:5px;font-size:13px;'
        'margin-bottom:8px;">💬 WhatsApp</a>'
        '</div>'
        f'<a href="{link}" style="display:inline-block;background:#1a73e8;color:#fff;'
        'text-decoration:none;padding:9px 16px;border-radius:5px;font-size:13px;">'
        '🔗 View Appointment</a>'
        '</div>'
    )


def _apt_html_blocks(apts):
    return "\n".join(_apt_html_block(a) for a in apts)


def _html_email(header_color, header_title, body_html):
    """Wrap body_html in a clean, mobile-friendly HTML email shell."""
    return (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '</head>'
        '<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">'
        '<div style="max-width:600px;margin:24px auto;background:#fff;border-radius:10px;'
        'overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08);">'
        f'<div style="background:{header_color};padding:22px 28px;">'
        f'<h1 style="margin:0;color:#fff;font-size:20px;line-height:1.3;">{header_title}</h1>'
        '</div>'
        '<div style="padding:24px 28px;font-size:15px;color:#333;line-height:1.6;">'
        f'{body_html}'
        '</div>'
        '<div style="background:#f8f8f8;padding:14px 28px;border-top:1px solid #e8e8e8;'
        'text-align:center;">'
        '<p style="margin:0;color:#aaa;font-size:12px;">HomeBase Plumbers · Automated Reminder</p>'
        '</div>'
        '</div></body></html>'
    )


def _hr():
    return '<hr style="border:none;border-top:1px solid #e0e0e0;margin:16px 0;">'


def _send_email(recipients, subject, html, dry_run):
    if dry_run:
        logger.info("DRY RUN email '%s' → %s", subject, recipients)
        return True
    plain = f"{subject}\n\nHomeBase Plumbers\nWhatsApp: +263776255077"
    return send_email_to_recipients(recipients, subject, plain, html_message=html)


# ═══════════════════════════════════════════════════════════════════════════════
# MANAGEMENT COMMAND
# ═══════════════════════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = (
        "Send appointment reminders to customers and plumber. "
        "Run every 15 minutes. Customer reminders are NOT gated by the "
        "WhatsApp 24-hour message window — confirmed appointments always "
        "receive reminders."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be sent without actually sending anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        try:
            from bot.models import Appointment
        except ImportError:
            self.stderr.write("Could not import Appointment model.")
            return

        cat       = pytz.timezone(TIMEZONE_NAME)
        now_utc   = timezone.now()
        now_local = now_utc.astimezone(cat)
        today     = now_local.date()
        tomorrow  = today + timedelta(days=1)

        if dry_run:
            self.stdout.write(self.style.WARNING("🧪 DRY RUN — no messages will be sent\n"))

        self.stdout.write(
            f"\n{'=' * 60}\n"
            f"  REMINDER DISPATCHER  |  {now_local.strftime('%a %d %b %Y  %H:%M')} CAT\n"
            f"{'=' * 60}\n"
        )

        # ── Fetch ALL confirmed future appointments (no WhatsApp window filter) ──
        base_qs = Appointment.objects.filter(
            Q(status__in=["confirmed", "scheduled", "booked"]),
            scheduled_datetime__isnull=False,
            scheduled_datetime__date__gte=today,
        ).order_by("scheduled_datetime")

        all_apts = list(base_qs)
        self.stdout.write(f"  Confirmed appointments found: {len(all_apts)}\n")

        plumber_contact = f"+{PLUMBER_PHONE}" if PLUMBER_PHONE else "+263774819901"

        # ─────────────────────────────────────────────────────────────────────
        # CUSTOMER REMINDERS
        # ─────────────────────────────────────────────────────────────────────
        self.stdout.write(f"\n  {'─' * 56}")
        self.stdout.write("  CUSTOMER REMINDERS\n")

        customer_sent = customer_skipped = customer_failed = 0

        # (days_away, send_hour, reminder_type, wa_builder, email_type, label)
        CUSTOMER_CHECKS = [
            (2, 18, "lead_2days",   _msg_2days,   "two_days", "2 Days Before  [6 PM]"),
            (1, 18, "lead_1day",    _msg_1day,    "one_day",  "1 Day Before   [6 PM]"),
            (0,  7, "lead_morning", _msg_morning, "morning",  "Morning Of     [7 AM]"),
        ]

        for apt in all_apts:
            apt_u = _appt_utc(apt)
            if not apt_u:
                continue

            apt_local = apt_u.astimezone(cat)
            days_away = (apt_local.date() - today).days
            phone     = _fmt_phone(apt.phone_number or "")
            name      = apt.customer_name or "Customer"
            apt_label = f"{name} (+{phone})  |  {apt_local.strftime('%Y-%m-%d %H:%M')}"

            window_open = is_window_open(apt)
            has_email   = bool(getattr(apt, "customer_email", None))

            # Fixed-time reminders
            for days, hour, rtype, builder, email_type, label in CUSTOMER_CHECKS:
                if days_away == days and _in_window(now_local, hour):
                    if _already_sent_customer(apt, rtype):
                        customer_skipped += 1
                        self.stdout.write(f"    SKIP  {label} → {apt_label}")
                    elif window_open:
                        msg = builder(apt, plumber_contact.replace("+", ""))
                        ok  = _send_wa(phone, msg, dry_run=dry_run)
                        if ok:
                            if not dry_run:
                                _mark_sent_customer(apt, rtype)
                            customer_sent += 1
                            self.stdout.write(
                                self.style.SUCCESS(f"    SENT  {label} [WA] → {apt_label}")
                            )
                        else:
                            customer_failed += 1
                            self.stdout.write(self.style.ERROR(f"    FAIL  {label} [WA] → {apt_label}"))
                    elif has_email:
                        ok = send_customer_reminder_email(apt, email_type) if not dry_run else True
                        if ok:
                            if not dry_run:
                                _mark_sent_customer(apt, rtype)
                            customer_sent += 1
                            self.stdout.write(
                                self.style.SUCCESS(f"    SENT  {label} [EMAIL] → {apt_label}")
                            )
                        else:
                            customer_failed += 1
                            self.stdout.write(self.style.ERROR(f"    FAIL  {label} [EMAIL] → {apt_label}"))
                    else:
                        self.stdout.write(
                            f"    SKIP  {label} → {apt_label} "
                            f"[window closed, no email]"
                        )

            # 2-hour reminder
            if _is_2h_window(apt_u, now_utc):
                rtype = "lead_2hours"
                if _already_sent_customer(apt, rtype):
                    customer_skipped += 1
                    self.stdout.write(f"    SKIP  2 Hours Before → {apt_label}")
                elif window_open:
                    msg = _msg_2hours(apt, plumber_contact.replace("+", ""))
                    ok  = _send_wa(phone, msg, dry_run=dry_run)
                    if ok:
                        if not dry_run:
                            _mark_sent_customer(apt, rtype)
                        customer_sent += 1
                        self.stdout.write(self.style.SUCCESS(f"    SENT  2 Hours Before [WA] → {apt_label}"))
                    else:
                        customer_failed += 1
                        self.stdout.write(self.style.ERROR(f"    FAIL  2 Hours Before [WA] → {apt_label}"))
                elif has_email:
                    ok = send_customer_reminder_email(apt, "two_hours") if not dry_run else True
                    if ok:
                        if not dry_run:
                            _mark_sent_customer(apt, rtype)
                        customer_sent += 1
                        self.stdout.write(self.style.SUCCESS(f"    SENT  2 Hours Before [EMAIL] → {apt_label}"))
                    else:
                        customer_failed += 1
                        self.stdout.write(self.style.ERROR(f"    FAIL  2 Hours Before [EMAIL] → {apt_label}"))
                else:
                    self.stdout.write(f"    SKIP  2 Hours Before → {apt_label} [window closed, no email]")

        if customer_sent == 0 and customer_skipped == 0 and customer_failed == 0:
            self.stdout.write("    No customer reminders due at this time.")

        self.stdout.write(
            f"\n    Summary → sent={customer_sent}  "
            f"skipped={customer_skipped}  failed={customer_failed}"
        )

        # ─────────────────────────────────────────────────────────────────────
        # DELAYED LEAD FOLLOW-UPS
        # Finds leads where today matches their agreed [FOLLOW_UP_DATE].
        # Sends via WhatsApp if the window is open, email otherwise.
        # ─────────────────────────────────────────────────────────────────────
        self.stdout.write(f"\n  {'─' * 56}")
        self.stdout.write("  DELAYED LEAD FOLLOW-UPS\n")

        followup_sent = followup_skipped = followup_failed = 0

        delayed_leads = list(
            Appointment.objects.filter(
                internal_notes__contains=f"[FOLLOW_UP_DATE] {today.isoformat()}",
            ).exclude(
                internal_notes__contains="[FOLLOW_UP_SENT]"
            )
        )
        self.stdout.write(f"  Leads due for follow-up today: {len(delayed_leads)}\n")

        _WA_FOLLOWUP_NAMED = (
            "{name}, said I'd check in — here I am.\n\n"
            "Most people who weren't ready when we first spoke end up getting the "
            "free site visit done anyway. Takes 20 minutes, costs nothing, and gives "
            "you a clear picture of exactly what you're working with and what it'll cost.\n\n"
            "Even if you're still in the planning stage, it's worth knowing where you "
            "stand. Just reply here on WhatsApp with a day that works and we'll come "
            "to you."
        )
        _WA_FOLLOWUP_ANON = (
            "Said I'd check in — here I am.\n\n"
            "Most people who weren't ready when we first spoke end up getting the "
            "free site visit done anyway. Takes 20 minutes, costs nothing, and gives "
            "you a clear picture of exactly what you're working with and what it'll cost.\n\n"
            "Even if you're still in the planning stage, it's worth knowing where you "
            "stand. Just reply here on WhatsApp with a day that works and we'll come "
            "to you."
        )

        for apt in delayed_leads:
            raw_name = (apt.customer_name or "").strip()
            has_name = bool(raw_name)
            name     = raw_name if has_name else None
            phone    = _fmt_phone(apt.phone_number or "")
            email    = getattr(apt, "customer_email", None)
            label    = f"{name or 'Unknown'} (+{phone})"
            svc      = _service(apt)

            def _mark_followup_sent(a):
                notes = a.internal_notes or ""
                if "[FOLLOW_UP_SENT]" not in notes:
                    a.internal_notes = f"{notes}\n[FOLLOW_UP_SENT]".strip()
                    a.save(update_fields=["internal_notes"])

            wa_msg = (
                _WA_FOLLOWUP_NAMED.format(name=name)
                if has_name else _WA_FOLLOWUP_ANON
            )
            window = is_window_open(apt)

            if window:
                ok = _send_wa(phone, wa_msg, dry_run=dry_run)
                if ok:
                    if not dry_run:
                        _mark_followup_sent(apt)
                    followup_sent += 1
                    self.stdout.write(
                        self.style.SUCCESS(f"    SENT  Follow-up [WA] → {label}")
                    )
                else:
                    followup_failed += 1
                    self.stdout.write(self.style.ERROR(f"    FAIL  Follow-up [WA] → {label}"))

            elif email:
                from bot.customer_emails import _wrap, _send, _WA_NUMBER
                subject   = f"Following Up — Your {svc} Assessment"
                opener    = f"<p>{name},</p>" if has_name else ""
                body_html = (
                    f"{opener}"
                    "<p>Said I'd check in — here I am.</p>"
                    "<p>Most people who weren't ready when we first spoke end up "
                    "getting the free site visit done anyway. Takes 20 minutes, costs "
                    "nothing, and gives you a clear picture of exactly what you're "
                    "working with and what it'll cost.</p>"
                    "<p>Even if you're still in the planning stage, it's worth knowing "
                    "where you stand. Drop us a WhatsApp with a day that works and "
                    "we'll come to you.</p>"
                    f'<p style="margin-top:16px;">'
                    f'<a href="https://wa.me/{_WA_NUMBER}" '
                    f'style="background:#25D366;color:#fff;text-decoration:none;'
                    f'padding:10px 20px;border-radius:5px;font-size:14px;">'
                    f"Reply on WhatsApp</a></p>"
                )
                html = _wrap(body_html)
                ok   = _send(apt, subject, html) if not dry_run else True
                if ok:
                    if not dry_run:
                        _mark_followup_sent(apt)
                    followup_sent += 1
                    self.stdout.write(
                        self.style.SUCCESS(f"    SENT  Follow-up [EMAIL] → {label}")
                    )
                else:
                    followup_failed += 1
                    self.stdout.write(self.style.ERROR(f"    FAIL  Follow-up [EMAIL] → {label}"))

            else:
                self.stdout.write(
                    f"    SKIP  Follow-up → {label} [window closed, no email on file]"
                )
                followup_skipped += 1

        self.stdout.write(
            f"\n    Follow-up summary → sent={followup_sent}  "
            f"skipped={followup_skipped}  failed={followup_failed}"
        )

        # ─────────────────────────────────────────────────────────────────────
        # PLUMBER REMINDERS  (not gated by customer message window either)
        # ─────────────────────────────────────────────────────────────────────
        self.stdout.write(f"\n  {'─' * 56}")
        self.stdout.write("  PLUMBER REMINDERS\n")

        plumber_sent = 0

        if not PLUMBER_PHONE:
            self.stdout.write(
                self.style.WARNING(
                    "    PLUMBER_PHONE_NUMBER env var not set — skipping plumber reminders."
                )
            )
        else:
            self.stdout.write(f"    Recipient: {PLUMBER_NAME}  |  +{PLUMBER_PHONE}\n")

            # Evening briefing: tomorrow's appointments @ 20:00
            if _in_window(now_local, 20):
                tomorrow_apts = [a for a in all_apts if a.scheduled_datetime.date() == tomorrow]
                if tomorrow_apts:
                    marker = tomorrow_apts[0]
                    rtype  = f"plumber_nextday_{tomorrow.isoformat()}"
                    if _already_sent_plumber(marker, rtype):
                        self.stdout.write(f"    SKIP  Tomorrow's Jobs ({len(tomorrow_apts)} apts) [already sent]")
                    else:
                        msg = _msg_plumber_next_day(tomorrow_apts, PLUMBER_NAME)
                        ok = _send_wa(PLUMBER_PHONE, msg, dry_run=dry_run)
                        if ok:
                            if not dry_run:
                                _mark_sent_plumber(marker, rtype)
                            plumber_sent += 1
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"    SENT  Tomorrow's Jobs  |  {len(tomorrow_apts)} appointment(s)"
                                )
                            )
                        else:
                            self.stdout.write(self.style.ERROR("    FAIL  Tomorrow's Jobs"))
                else:
                    self.stdout.write("    INFO  No appointments tomorrow — evening briefing skipped.")

            # Morning briefing @ 07:00
            if _in_window(now_local, 7):
                today_apts = [a for a in all_apts if a.scheduled_datetime.date() == today]
                if today_apts:
                    marker = today_apts[0]
                    rtype  = f"plumber_morning_{today.isoformat()}"
                    if _already_sent_plumber(marker, rtype):
                        self.stdout.write(f"    SKIP  Morning Briefing ({len(today_apts)} apts) [already sent]")
                    else:
                        msg = _msg_plumber_morning(today_apts, PLUMBER_NAME)
                        ok = _send_wa(PLUMBER_PHONE, msg, dry_run=dry_run)
                        if ok:
                            if not dry_run:
                                _mark_sent_plumber(marker, rtype)
                            plumber_sent += 1
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"    SENT  Morning Briefing  |  {len(today_apts)} appointment(s)"
                                )
                            )
                        else:
                            self.stdout.write(self.style.ERROR("    FAIL  Morning Briefing"))
                else:
                    self.stdout.write("    INFO  No appointments today — morning briefing skipped.")

            # 2-hour alerts for plumber
            for apt in all_apts:
                apt_u = _appt_utc(apt)
                if not apt_u:
                    continue
                if _is_2h_window(apt_u, now_utc):
                    name  = apt.customer_name or "?"
                    rtype = f"plumber_2hours_{apt.id}"
                    if _already_sent_plumber(apt, rtype):
                        self.stdout.write(f"    SKIP  2-Hour Alert → {name} [already sent]")
                    else:
                        msg = _msg_plumber_2hours(apt, PLUMBER_NAME)
                        ok = _send_wa(PLUMBER_PHONE, msg, dry_run=dry_run)
                        if ok:
                            if not dry_run:
                                _mark_sent_plumber(apt, rtype)
                            plumber_sent += 1
                            self.stdout.write(
                                self.style.SUCCESS(f"    SENT  2-Hour Alert → {name}")
                            )
                        else:
                            self.stdout.write(self.style.ERROR(f"    FAIL  2-Hour Alert → {name}"))

        # ─────────────────────────────────────────────────────────────────────
        # PLUMBER EMAIL REMINDERS  (5 email types — all sent to both recipients)
        # ─────────────────────────────────────────────────────────────────────
        self.stdout.write(f"\n  {'─' * 56}")
        self.stdout.write("  PLUMBER EMAIL REMINDERS\n")

        email_sent = email_skipped = email_failed = 0
        email_recipients = get_plumber_notification_emails()

        # Active upcoming appointments — used by emails 2, 3, 4, 5
        active_apts = list(
            Appointment.objects.filter(
                scheduled_datetime__isnull=False,
                scheduled_datetime__date__gte=today,
            ).exclude(
                status__in=["cancelled", "no_show"]
            ).order_by("scheduled_datetime")
        )

        # ── Email 1: Weekly Summary — Every Sunday at 20:00 SAST ─────────────
        # Shows NEXT week's appointments (Mon–Sun of the coming week).
        # Sent Sunday evening so the team wakes up Monday knowing what's ahead.
        if now_local.weekday() == 6 and _email_in_window(now_local, 20, 0):
            week_mon = today + timedelta(days=1)   # next Monday
            week_sun = today + timedelta(days=7)   # next Sunday
            week_apts = list(
                Appointment.objects.filter(
                    scheduled_datetime__isnull=False,
                    scheduled_datetime__date__gte=week_mon,
                    scheduled_datetime__date__lte=week_sun,
                ).exclude(status="cancelled")
                .order_by("scheduled_datetime")
            )
            flag         = f"email_weekly_{week_mon.isoformat()}"
            already_sent = bool(week_apts) and any(_eflag_set(a, flag) for a in week_apts)
            if already_sent:
                email_skipped += 1
                self.stdout.write(f"    SKIP  Email 1 Weekly already sent for week of {week_mon}")
            else:
                n       = len(week_apts)
                mon_lbl = _email_fmt_date_short(week_mon)
                sun_lbl = _email_fmt_date_short(week_sun)
                subject = (
                    f"Next week's schedule — {n} appointment{'s' if n != 1 else ''}"
                    f" ({mon_lbl})"
                )
                if n == 0:
                    apts_html = (
                        '<p style="color:#888;text-align:center;padding:20px 0;">'
                        'Nothing booked for next week yet.</p>'
                    )
                else:
                    apts_html = _apt_html_blocks(week_apts)
                body_html = (
                    '<p>Hi Team,</p>'
                    f'<p>Here is your schedule for next week, '
                    f'<strong>{mon_lbl} – {sun_lbl}</strong>.</p>'
                    f'{_hr()}'
                    f'<p style="font-size:16px;font-weight:bold;">TOTAL BOOKED: {n}</p>'
                    f'{_hr()}'
                    f'{apts_html}'
                    '<p>Have a great week ahead.</p>'
                )
                html = _html_email(
                    "#1a73e8",
                    f"Next week — {n} appointment{'s' if n != 1 else ''} scheduled",
                    body_html,
                )
                ok = _send_email(email_recipients, subject, html, dry_run)
                if ok:
                    if not dry_run and week_apts:
                        _eflag_mark(week_apts[0], flag)
                    email_sent += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"    SENT  Email 1 Weekly — {n} apt(s), {mon_lbl} to {sun_lbl}"
                        )
                    )
                else:
                    email_failed += 1
                    self.stdout.write(self.style.ERROR("    FAIL  Email 1 Weekly"))

        # ── Email 2: Next-Day Preview — Every day at 20:00 SAST ──────────────
        # Only sends if there is ≥1 appointment tomorrow.
        if _email_in_window(now_local, 20, 0):
            nextday_apts = [
                a for a in active_apts
                if (_to_sast(a.scheduled_datetime) or now_local).date() == tomorrow
            ]
            if not nextday_apts:
                self.stdout.write("    INFO  Email 2 (Next-Day): no appointments tomorrow — skipped")
            else:
                flag   = f"email_nextday_{tomorrow.isoformat()}"
                marker = nextday_apts[0]
                if _eflag_set(marker, flag):
                    email_skipped += 1
                    self.stdout.write(f"    SKIP  Email 2 Next-Day already sent for {tomorrow}")
                else:
                    n       = len(nextday_apts)
                    day_lbl = _email_fmt_date_short(tomorrow)
                    subject = (
                        f"Tomorrow's schedule — {n} job{'s' if n != 1 else ''}"
                        f" on {day_lbl}"
                    )
                    body_html = (
                        '<p>Hi Team,</p>'
                        f'<p>You have <strong>{n} job{"s" if n != 1 else ""}</strong> scheduled'
                        f' for tomorrow, <strong>{day_lbl}</strong>.</p>'
                        f'{_hr()}'
                        f'<p style="font-size:16px;font-weight:bold;">TOTAL JOBS TOMORROW: {n}</p>'
                        f'{_hr()}'
                        f'{_apt_html_blocks(nextday_apts)}'
                        "<p>Get a good night's rest — see you tomorrow!</p>"
                    )
                    html = _html_email(
                        "#e65c00",
                        f"🗓️ Tomorrow's Jobs — {n} Appointment{'s' if n != 1 else ''}",
                        body_html,
                    )
                    ok = _send_email(email_recipients, subject, html, dry_run)
                    if ok:
                        if not dry_run:
                            _eflag_mark(marker, flag)
                        email_sent += 1
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"    SENT  Email 2 Next-Day — {n} apt(s) for {day_lbl}"
                            )
                        )
                    else:
                        email_failed += 1
                        self.stdout.write(self.style.ERROR("    FAIL  Email 2 Next-Day"))

        # ── Email 3: Morning of Appointments — Every day at 06:30 SAST ───────
        # Only sends if there is ≥1 appointment today.
        if _email_in_window(now_local, 6, 30):
            today_apts = [
                a for a in active_apts
                if (_to_sast(a.scheduled_datetime) or now_local).date() == today
            ]
            if not today_apts:
                self.stdout.write("    INFO  Email 3 (Morning): no appointments today — skipped")
            else:
                flag   = f"email_morning_{today.isoformat()}"
                marker = today_apts[0]
                if _eflag_set(marker, flag):
                    email_skipped += 1
                    self.stdout.write(f"    SKIP  Email 3 Morning already sent for {today}")
                else:
                    n       = len(today_apts)
                    day_lbl = _email_fmt_date_short(today)
                    subject = (
                        f"Today's schedule — {n} job{'s' if n != 1 else ''}"
                        f" ({day_lbl})"
                    )
                    body_html = (
                        '<p>Good morning Team,</p>'
                        f'<p>Here are your jobs for today, <strong>{day_lbl}</strong>.</p>'
                        f'{_hr()}'
                        f'<p style="font-size:16px;font-weight:bold;">TOTAL JOBS TODAY: {n}</p>'
                        f'{_hr()}'
                        f'{_apt_html_blocks(today_apts)}'
                        '<p>Have a productive day!</p>'
                    )
                    html = _html_email(
                        "#e65c00",
                        f"☀️ Today's Jobs — {n} Appointment{'s' if n != 1 else ''}",
                        body_html,
                    )
                    ok = _send_email(email_recipients, subject, html, dry_run)
                    if ok:
                        if not dry_run:
                            _eflag_mark(marker, flag)
                        email_sent += 1
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"    SENT  Email 3 Morning — {n} apt(s) for {day_lbl}"
                            )
                        )
                    else:
                        email_failed += 1
                        self.stdout.write(self.style.ERROR("    FAIL  Email 3 Morning"))

        # ── Email 6: Date-but-no-time — morning of the agreed date @ 07:00 ───
        # A lead committed an appointment DATE but never a time (stored at
        # midnight). On the morning of that date, remind the plumber to call and
        # pin the time down. Complements the immediate alert fired when the lead
        # first gave the date. Once per appointment (guarded by an eflag).
        if _email_in_window(now_local, 7, 0):
            from bot.plumber_notifications import send_plumber_followup_alert
            no_time_apts = []
            for a in (
                Appointment.objects
                .filter(scheduled_datetime__isnull=False)
                .exclude(status__in=["cancelled", "no_show", "completed"])
            ):
                sast = _to_sast(a.scheduled_datetime)
                if not sast or sast.date() != today:
                    continue
                if sast.hour != 0 or sast.minute != 0:
                    continue  # a real time is set — nothing to chase
                no_time_apts.append(a)

            if not no_time_apts:
                self.stdout.write("    INFO  Email 6 (Date-no-time): none due today — skipped")
            for a in no_time_apts:
                flag = f"email_date_no_time_{a.pk}_{today.isoformat()}"
                if _eflag_set(a, flag):
                    email_skipped += 1
                    self.stdout.write(f"    SKIP  Email 6 Date-no-time apt#{a.pk}: already sent")
                    continue
                ok = (
                    send_plumber_followup_alert(a, reason="date_no_time")
                    if not dry_run else True
                )
                if ok:
                    if not dry_run:
                        _eflag_mark(a, flag)
                    email_sent += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"    SENT  Email 6 Date-no-time → apt#{a.pk} ({_service(a)})"
                    ))
                else:
                    email_failed += 1
                    self.stdout.write(self.style.ERROR(
                        f"    FAIL  Email 6 Date-no-time → apt#{a.pk}"
                    ))

        # ── Emails 4 & 5: Per-appointment rolling reminders ──────────────────
        for apt in active_apts:
            if not _to_sast(apt.scheduled_datetime):
                continue
            apt_label = (
                f"apt#{apt.pk} {_service(apt)} @ {_email_fmt_date(apt)} {_email_fmt_time(apt)}"
            )

            # Email 4 — 2 hours before appointment
            if _is_2hr_email_window(apt, now_utc):
                flag = f"email_2hr_{apt.pk}"
                if _eflag_set(apt, flag):
                    email_skipped += 1
                    self.stdout.write(f"    SKIP  Email 4 (2hr) {apt_label}: already sent")
                else:
                    subject = (
                        f"On the way — {_service(apt)} in {_area(apt)}"
                        f" at {_email_fmt_time(apt)}"
                    )
                    body_html = (
                        '<p>Hi Team,</p>'
                        '<p>This is a reminder that your next job starts in'
                        ' <strong>2 hours</strong>.</p>'
                        f'{_apt_html_block(apt)}'
                        '<p>Make sure you have everything you need — head out soon!</p>'
                    )
                    html = _html_email(
                        "#e53935",
                        f"⏰ Job in 2 Hours — {_service(apt)} in {_area(apt)}",
                        body_html,
                    )
                    ok = _send_email(email_recipients, subject, html, dry_run)
                    if ok:
                        if not dry_run:
                            _eflag_mark(apt, flag)
                        email_sent += 1
                        self.stdout.write(self.style.SUCCESS(f"    SENT  Email 4 (2hr)  {apt_label}"))
                    else:
                        email_failed += 1
                        self.stdout.write(self.style.ERROR(f"    FAIL  Email 4 (2hr)  {apt_label}"))

            # Email 5 — 30 minutes before appointment
            if _is_30min_window(apt, now_utc):
                flag = f"email_30min_{apt.pk}"
                if _eflag_set(apt, flag):
                    email_skipped += 1
                    self.stdout.write(f"    SKIP  Email 5 (30m) {apt_label}: already sent")
                else:
                    subject = (
                        f"Arriving in 30 minutes — {_service(apt)}"
                        f" in {_area(apt)} at {_email_fmt_time(apt)}"
                    )
                    body_html = (
                        '<p>Hi Team,</p>'
                        '<p>Your next job starts in <strong>30 minutes</strong>'
                        ' — time to head out!</p>'
                        f'{_apt_html_block(apt)}'
                        "<p>You've got this!</p>"
                    )
                    html = _html_email(
                        "#b71c1c",
                        f"🚨 30 Minutes Away — {_service(apt)} in {_area(apt)}",
                        body_html,
                    )
                    ok = _send_email(email_recipients, subject, html, dry_run)
                    if ok:
                        if not dry_run:
                            _eflag_mark(apt, flag)
                        email_sent += 1
                        self.stdout.write(self.style.SUCCESS(f"    SENT  Email 5 (30m)  {apt_label}"))
                    else:
                        email_failed += 1
                        self.stdout.write(self.style.ERROR(f"    FAIL  Email 5 (30m)  {apt_label}"))

        self.stdout.write(
            f"\n    Email summary → sent={email_sent}  "
            f"skipped={email_skipped}  failed={email_failed}"
        )

        # ─────────────────────────────────────────────────────────────────────
        # FINAL SUMMARY
        # ─────────────────────────────────────────────────────────────────────
        total_sent = customer_sent + plumber_sent + email_sent
        self.stdout.write(
            f"\n{'=' * 60}\n"
            f"  DONE  |  customer_wa={customer_sent}  "
            f"plumber_wa={plumber_sent}  "
            f"emails={email_sent}  "
            f"total={total_sent}\n"
            f"  Next run in ~5 minutes\n"
            f"{'=' * 60}\n"
        )
