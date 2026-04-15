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
                        ok  = _send_wa(PLUMBER_PHONE, msg, dry_run=dry_run)
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
                        ok  = _send_wa(PLUMBER_PHONE, msg, dry_run=dry_run)
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
                        ok  = _send_wa(PLUMBER_PHONE, msg, dry_run=dry_run)
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
        # FINAL SUMMARY
        # ─────────────────────────────────────────────────────────────────────
        total_sent = customer_sent + plumber_sent
        self.stdout.write(
            f"\n{'=' * 60}\n"
            f"  DONE  |  customer_sent={customer_sent}  "
            f"plumber_sent={plumber_sent}  "
            f"total={total_sent}\n"
            f"  Next run in ~15 minutes\n"
            f"{'=' * 60}\n"
        )