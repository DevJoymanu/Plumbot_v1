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
from collections import defaultdict

import pytz
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Q

from django.conf import settings
from django.urls import reverse

from bot.plumber_notifications import (
    get_plumber_notification_emails,
    send_email_to_recipients,
    send_plumber_notification_email,
)

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

_SAST       = pytz.timezone("Africa/Johannesburg")
_SEP        = "────────────────────────"
_SITE_URL   = getattr(settings, "SITE_URL", "").rstrip("/")
_WIN_EMAIL  = 8   # ±8 min tolerance for email scheduled-send windows

_DAYS_LONG   = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
_DAYS_SHORT  = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
_MONTHS      = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]


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
    return f"{_DAYS_LONG[d.weekday()]}, {d.day} {_MONTHS[d.month - 1]} {d.year}"


def _email_fmt_date_short(d):
    return f"{_DAYS_SHORT[d.weekday()]}, {d.day} {_MONTHS[d.month - 1]} {d.year}"


def _clean_phone(apt):
    return "".join(c for c in (apt.phone_number or "") if c.isdigit())


def _apt_deep_link(apt):
    try:
        path = reverse("appointment_detail", kwargs={"pk": apt.pk})
        return f"{_SITE_URL}{path}" if _SITE_URL else f"/appointments/{apt.pk}/"
    except Exception:
        return f"/appointments/{apt.pk}/"


def _email_recipients(apt):
    plumber = getattr(apt, "assigned_plumber", None)
    if plumber and getattr(plumber, "email", ""):
        return [plumber.email]
    return get_plumber_notification_emails()


def _email_plumber_key(apt):
    plumber = getattr(apt, "assigned_plumber", None)
    return str(plumber.pk) if plumber else "global"


def _email_group_by_plumber(apts):
    groups = defaultdict(list)
    for a in apts:
        groups[_email_plumber_key(a)].append(a)
    return dict(groups)


def _eflag_set(apt, key):
    return f"[{key}]" in (apt.internal_notes or "")


def _eflag_mark(apt, key):
    existing = apt.internal_notes or ""
    token = f"[{key}]"
    if token not in existing:
        apt.internal_notes = f"{existing}\n{token}".strip()
        apt.save(update_fields=["internal_notes"])


def _email_apt_block(apt):
    clean = _clean_phone(apt)
    return (
        f"{_SEP}\n"
        f"📅 {_email_fmt_date(apt)} at {_email_fmt_time(apt)}\n"
        f"🔧 Service:   {_service(apt)}\n"
        f"📍 Area:      {_area(apt)}\n"
        f"📞 Call:      tel:+{clean}\n"
        f"💬 WhatsApp:  https://wa.me/{clean}\n"
        f"🔗 View:      {_apt_deep_link(apt)}\n"
        f"{_SEP}"
    )


def _email_blocks(apts):
    return "\n\n".join(_email_apt_block(a) for a in apts)


def _n_bookings(n):
    return f"{n} Booking{'s' if n != 1 else ''}"


def _send_email(recipients, subject, body, dry_run):
    if dry_run:
        logger.info("DRY RUN email '%s' → %s", subject, recipients)
        return True
    return send_email_to_recipients(recipients, subject, body)


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

        # (days_away, send_hour, reminder_type, builder, label)
        CUSTOMER_CHECKS = [
            (2, 18, "lead_2days",   _msg_2days,   "2 Days Before  [6 PM]"),
            (1, 18, "lead_1day",    _msg_1day,    "1 Day Before   [6 PM]"),
            (0,  7, "lead_morning", _msg_morning, "Morning Of     [7 AM]"),
        ]

        for apt in all_apts:
            apt_u = _appt_utc(apt)
            if not apt_u:
                continue

            apt_local  = apt_u.astimezone(cat)
            days_away  = (apt_local.date() - today).days
            phone      = _fmt_phone(apt.phone_number or "")
            name       = apt.customer_name or "Customer"
            apt_label  = f"{name} (+{phone})  |  {apt_local.strftime('%Y-%m-%d %H:%M')}"

            # Fixed-time reminders
            for days, hour, rtype, builder, label in CUSTOMER_CHECKS:
                if days_away == days and _in_window(now_local, hour):
                    if _already_sent_customer(apt, rtype):
                        customer_skipped += 1
                        self.stdout.write(f"    SKIP  {label} → {apt_label}")
                    else:
                        msg = builder(apt, plumber_contact.replace("+", ""))
                        ok  = _send_wa(phone, msg, dry_run=dry_run)
                        if ok:
                            if not dry_run:
                                _mark_sent_customer(apt, rtype)
                            customer_sent += 1
                            self.stdout.write(
                                self.style.SUCCESS(f"    SENT  {label} → {apt_label}")
                            )
                        else:
                            customer_failed += 1
                            self.stdout.write(
                                self.style.ERROR(f"    FAIL  {label} → {apt_label}")
                            )

            # 2-hour reminder
            if _is_2h_window(apt_u, now_utc):
                rtype = "lead_2hours"
                if _already_sent_customer(apt, rtype):
                    customer_skipped += 1
                    self.stdout.write(f"    SKIP  2 Hours Before → {apt_label}")
                else:
                    msg = _msg_2hours(apt, plumber_contact.replace("+", ""))
                    ok  = _send_wa(phone, msg, dry_run=dry_run)
                    if ok:
                        if not dry_run:
                            _mark_sent_customer(apt, rtype)
                        customer_sent += 1
                        self.stdout.write(
                            self.style.SUCCESS(f"    SENT  2 Hours Before → {apt_label}")
                        )
                    else:
                        customer_failed += 1
                        self.stdout.write(
                            self.style.ERROR(f"    FAIL  2 Hours Before → {apt_label}")
                        )

        if customer_sent == 0 and customer_skipped == 0 and customer_failed == 0:
            self.stdout.write("    No customer reminders due at this time.")

        self.stdout.write(
            f"\n    Summary → sent={customer_sent}  "
            f"skipped={customer_skipped}  failed={customer_failed}"
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
        # PLUMBER EMAIL REMINDERS  (all 5 email types)
        # ─────────────────────────────────────────────────────────────────────
        self.stdout.write(f"\n  {'─' * 56}")
        self.stdout.write("  PLUMBER EMAIL REMINDERS\n")

        email_sent = email_skipped = email_failed = 0

        # Appointments from today onward, non-cancelled, with scheduled time
        email_apts = list(
            Appointment.objects.filter(
                scheduled_datetime__isnull=False,
                scheduled_datetime__date__gte=today,
            ).exclude(
                status__in=["cancelled", "completed", "no_show"]
            ).select_related("assigned_plumber")
            .order_by("scheduled_datetime")
        )

        week_start = today - timedelta(days=today.weekday())   # Monday
        week_end   = week_start + timedelta(days=6)

        # ── Email 1: Weekly Summary — Monday 07:00 SAST ──────────────────────
        if now_local.weekday() == 0 and _email_in_window(now_local, 7, 0):
            week_apts = [
                a for a in email_apts
                if week_start <= (_to_sast(a.scheduled_datetime) or now_local).date() <= week_end
            ]
            if not week_apts:
                self.stdout.write("    INFO  Email 1 (Weekly): no appointments this week")
            else:
                for pkey, grp in _email_group_by_plumber(week_apts).items():
                    marker = grp[0]
                    flag   = f"email_weekly_{week_start.isoformat()}_{pkey}"
                    if _eflag_set(marker, flag):
                        email_skipped += 1
                        self.stdout.write(f"    SKIP  Email 1 Weekly [{pkey}] already sent")
                        continue
                    week_label = _email_fmt_date_short(week_start)
                    subject = (
                        f"Your Appointments for the Week of {week_label}"
                        f" — {_n_bookings(len(grp))}"
                    )
                    body = (
                        f"You have {len(grp)} appointment{'s' if len(grp) != 1 else ''} this week.\n\n"
                        f"{_email_blocks(grp)}"
                    )
                    ok = _send_email(_email_recipients(grp[0]), subject, body, dry_run)
                    if ok:
                        if not dry_run:
                            _eflag_mark(marker, flag)
                        email_sent += 1
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"    SENT  Email 1 Weekly [{pkey}] {len(grp)} apt(s)"
                            )
                        )
                    else:
                        email_failed += 1
                        self.stdout.write(self.style.ERROR(f"    FAIL  Email 1 Weekly [{pkey}]"))

        # ── Email 2: Next-Day Preview — Daily 20:00 SAST ─────────────────────
        if _email_in_window(now_local, 20, 0):
            nextday_apts = [
                a for a in email_apts
                if (_to_sast(a.scheduled_datetime) or now_local).date() == tomorrow
            ]
            if not nextday_apts:
                self.stdout.write("    INFO  Email 2 (Next-Day): no appointments tomorrow")
            else:
                for pkey, grp in _email_group_by_plumber(nextday_apts).items():
                    marker = grp[0]
                    flag   = f"email_nextday_{tomorrow.isoformat()}_{pkey}"
                    if _eflag_set(marker, flag):
                        email_skipped += 1
                        self.stdout.write(f"    SKIP  Email 2 Next-Day [{pkey}] already sent")
                        continue
                    day_label = _email_fmt_date_short(tomorrow)
                    subject = f"Tomorrow's Appointments — {_n_bookings(len(grp))} on {day_label}"
                    body = (
                        f"You have {len(grp)} appointment{'s' if len(grp) != 1 else ''} tomorrow.\n\n"
                        f"{_email_blocks(grp)}"
                    )
                    ok = _send_email(_email_recipients(grp[0]), subject, body, dry_run)
                    if ok:
                        if not dry_run:
                            _eflag_mark(marker, flag)
                        email_sent += 1
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"    SENT  Email 2 Next-Day [{pkey}] {len(grp)} apt(s)"
                            )
                        )
                    else:
                        email_failed += 1
                        self.stdout.write(self.style.ERROR(f"    FAIL  Email 2 Next-Day [{pkey}]"))

        # ── Email 3: Morning Of — Daily 06:30 SAST ───────────────────────────
        if _email_in_window(now_local, 6, 30):
            today_apts = [
                a for a in email_apts
                if (_to_sast(a.scheduled_datetime) or now_local).date() == today
            ]
            if not today_apts:
                self.stdout.write("    INFO  Email 3 (Morning): no appointments today")
            else:
                for pkey, grp in _email_group_by_plumber(today_apts).items():
                    marker = grp[0]
                    flag   = f"email_morning_{today.isoformat()}_{pkey}"
                    if _eflag_set(marker, flag):
                        email_skipped += 1
                        self.stdout.write(f"    SKIP  Email 3 Morning [{pkey}] already sent")
                        continue
                    day_label = _email_fmt_date_short(today)
                    subject = (
                        f"Today's Appointments — {_n_bookings(len(grp))} for Today, {day_label}"
                    )
                    body = (
                        f"Good morning! Here are your {len(grp)} appointment"
                        f"{'s' if len(grp) != 1 else ''} for today.\n\n"
                        f"{_email_blocks(grp)}"
                    )
                    ok = _send_email(_email_recipients(grp[0]), subject, body, dry_run)
                    if ok:
                        if not dry_run:
                            _eflag_mark(marker, flag)
                        email_sent += 1
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"    SENT  Email 3 Morning [{pkey}] {len(grp)} apt(s)"
                            )
                        )
                    else:
                        email_failed += 1
                        self.stdout.write(self.style.ERROR(f"    FAIL  Email 3 Morning [{pkey}]"))

        # ── Emails 4 & 5: Per-appointment rolling reminders ──────────────────
        for apt in email_apts:
            dt_sast = _to_sast(apt.scheduled_datetime)
            if not dt_sast:
                continue
            apt_label = f"apt#{apt.pk} {_service(apt)} @ {_email_fmt_date(apt)} {_email_fmt_time(apt)}"

            # Email 4 — 2-hour reminder
            if _is_2hr_email_window(apt, now_utc):
                mins_away = (dt_sast - now_utc).total_seconds() / 60
                if mins_away < 30:
                    self.stdout.write(f"    SKIP  Email 4 (2hr) {apt_label}: <30 min away")
                else:
                    flag = f"email_2hr_{apt.pk}"
                    if _eflag_set(apt, flag):
                        email_skipped += 1
                        self.stdout.write(f"    SKIP  Email 4 (2hr) {apt_label}: already sent")
                    else:
                        subject = (
                            f"Reminder: {_service(apt)} in {_area(apt)}"
                            f" at {_email_fmt_time(apt)} — in 2 hours"
                        )
                        body = f"Your next appointment is in 2 hours.\n\n{_email_apt_block(apt)}"
                        ok = _send_email(_email_recipients(apt), subject, body, dry_run)
                        if ok:
                            if not dry_run:
                                _eflag_mark(apt, flag)
                            email_sent += 1
                            self.stdout.write(
                                self.style.SUCCESS(f"    SENT  Email 4 (2hr)  {apt_label}")
                            )
                        else:
                            email_failed += 1
                            self.stdout.write(self.style.ERROR(f"    FAIL  Email 4 (2hr)  {apt_label}"))

            # Email 5 — 30-minute reminder
            if _is_30min_window(apt, now_utc):
                flag = f"email_30min_{apt.pk}"
                if _eflag_set(apt, flag):
                    email_skipped += 1
                    self.stdout.write(f"    SKIP  Email 5 (30m) {apt_label}: already sent")
                else:
                    subject = (
                        f"Reminder: {_service(apt)} in {_area(apt)}"
                        f" at {_email_fmt_time(apt)} — in 30 minutes"
                    )
                    body = (
                        f"Your appointment is in 30 minutes — time to head out!\n\n"
                        f"{_email_apt_block(apt)}"
                    )
                    ok = _send_email(_email_recipients(apt), subject, body, dry_run)
                    if ok:
                        if not dry_run:
                            _eflag_mark(apt, flag)
                        email_sent += 1
                        self.stdout.write(
                            self.style.SUCCESS(f"    SENT  Email 5 (30m)  {apt_label}")
                        )
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
