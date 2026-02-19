"""
Django Management Command: send_reminders
=========================================
Run every 15 minutes via Railway Scheduler or cron:

    python manage.py send_reminders

Cron:
    */15 * * * * cd /app && python manage.py send_reminders >> /var/log/reminders.log 2>&1
"""

import os
import json
import logging
from datetime import timedelta, date, time as dt_time
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Q

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
WINDOW_MINUTES = 10
PLUMBER_PHONE  = os.environ.get("PLUMBER_PHONE_NUMBER", "").replace("+", "").strip()
PLUMBER_NAME   = os.environ.get("PLUMBER_NAME", "there")
TIMEZONE_NAME  = "Africa/Harare"

# ═══════════════════════════════════════════════════════════════════════════════
# WHATSAPP MESSAGE TEMPLATES
# ═══════════════════════════════════════════════════════════════════════════════

SEP = "────────────────"


def _fmt_phone(raw: str) -> str:
    return raw.replace("whatsapp:+", "").replace("whatsapp:", "").replace("+", "").strip()


def _service(apt) -> str:
    svc = getattr(apt, "project_type", "") or ""
    return svc.replace("_", " ").title() or "Plumbing work"


def _area(apt) -> str:
    return getattr(apt, "customer_area", "") or "Your area"


def _apt_time(apt) -> str:
    t = getattr(apt, "appointment_time", "") or ""
    if not t:
        return "Scheduled time"
    try:
        parts = str(t).split(":")
        h, m = int(parts[0]), int(parts[1])
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {suffix}"
    except Exception:
        return str(t)


def _apt_date(apt) -> str:
    d = getattr(apt, "appointment_date", None)
    if not d:
        return "Scheduled date"
    try:
        import datetime
        if isinstance(d, str):
            d = datetime.date.fromisoformat(d)
        days = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        return f"{days[d.weekday()]} {d.day} {months[d.month-1]} {d.year}"
    except Exception:
        return str(d)


def _apt_date_short(apt) -> str:
    d = getattr(apt, "appointment_date", None)
    if not d:
        return ""
    try:
        import datetime
        if isinstance(d, str):
            d = datetime.date.fromisoformat(d)
        days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        return f"{days[d.weekday()]} {d.day} {months[d.month-1]} {d.year}"
    except Exception:
        return str(d)


# ── Customer (Lead) Messages ───────────────────────────────────────────────────

def msg_lead_2days(apt, plumber_phone: str = "") -> str:
    name    = getattr(apt, "customer_name", "") or "there"
    contact = plumber_phone or "+263610318200"
    return (
        f"Hi {name} 👋\n"
        f"\n"
        f"Just a friendly reminder about your upcoming appointment:\n"
        f"\n"
        f"🛠 Service: {_service(apt)}\n"
        f"📍 Location: {_area(apt)}\n"
        f"📅 Date: {_apt_date(apt)}\n"
        f"⏰ Time: {_apt_time(apt)}\n"
        f"\n"
        f"Please make sure someone is home and the work area is accessible.\n"
        f"\n"
        f"We look forward to assisting you! 🔧\n"
        f"\n"
        f"📞 Questions? Call us: {contact}"
    )


def msg_lead_1day(apt, plumber_phone: str = "") -> str:
    name    = getattr(apt, "customer_name", "") or "there"
    contact = plumber_phone or "+263610318200"
    return (
        f"Hi {name} 👋\n"
        f"\n"
        f"Your appointment is *tomorrow!*\n"
        f"\n"
        f"🛠 {_service(apt)}\n"
        f"📅 {_apt_date(apt)}\n"
        f"⏰ {_apt_time(apt)}\n"
        f"\n"
        f"Please ensure:\n"
        f"✅ Someone is home\n"
        f"✅ The work area is accessible\n"
        f"✅ Water can be shut off if needed\n"
        f"\n"
        f"See you tomorrow! 🔧\n"
        f"\n"
        f"📞 {contact}"
    )


def msg_lead_morning(apt, plumber_phone: str = "") -> str:
    name    = getattr(apt, "customer_name", "") or "there"
    contact = plumber_phone or "+263610318200"
    return (
        f"Good morning {name} ☀️\n"
        f"\n"
        f"Today is your appointment day!\n"
        f"\n"
        f"⏰ Arrival Time: {_apt_time(apt)}\n"
        f"📍 Location: {_area(apt)}\n"
        f"\n"
        f"Our plumber will be there on time.\n"
        f"Please make sure someone is available.\n"
        f"\n"
        f"See you shortly! 🔧\n"
        f"\n"
        f"📞 {contact}"
    )


def msg_lead_2hours(apt, plumber_phone: str = "") -> str:
    name    = getattr(apt, "customer_name", "") or "there"
    contact = plumber_phone or "+263610318200"
    return (
        f"Hi {name} ⏰\n"
        f"\n"
        f"Your plumber will be arriving in approximately *2 hours.*\n"
        f"\n"
        f"📅 Today\n"
        f"⏰ {_apt_time(apt)}\n"
        f"\n"
        f"Please ensure access is ready.\n"
        f"\n"
        f"See you soon! 🔧\n"
        f"\n"
        f"📞 {contact}"
    )


# ── Plumber Messages ───────────────────────────────────────────────────────────

def _apt_block(i: int, apt, show_date: bool = True) -> str:
    phone     = _fmt_phone(getattr(apt, "phone_number", "") or "")
    name      = getattr(apt, "customer_name", "?") or "?"
    date_part = f"{_apt_date_short(apt)} | " if show_date else ""
    return (
        f"{i}\ufe0f\u20e3 {name}\n"
        f"\U0001f6e0 {_service(apt)}\n"
        f"\U0001f4cd {_area(apt)}\n"
        f"\u23f0 {date_part}{_apt_time(apt)}\n"
        f"\U0001f4de {phone}"
    )


def msg_plumber_weekly(apts: list, plumber_name: str = "there") -> str:
    blocks = f"\n{SEP}\n\n".join(_apt_block(i + 1, a, show_date=True) for i, a in enumerate(apts))
    return (
        f"\U0001f4c5 *WEEKLY APPOINTMENT SUMMARY*\n"
        f"\n"
        f"Hi {plumber_name} \U0001f44b\n"
        f"Here are your upcoming jobs:\n"
        f"\n"
        f"{SEP}\n"
        f"\n"
        f"{blocks}\n"
        f"\n"
        f"{SEP}\n"
        f"\n"
        f"Please review your schedule and prepare materials accordingly.\n"
        f"\n"
        f"Let's have a productive week! \U0001f4aa\U0001f527"
    )


def msg_plumber_next_day(apts: list, plumber_name: str = "there") -> str:
    blocks = f"\n{SEP}\n\n".join(_apt_block(i + 1, a, show_date=False) for i, a in enumerate(apts))
    return (
        f"\U0001f319 *TOMORROW'S APPOINTMENTS*\n"
        f"\n"
        f"Hi {plumber_name} \U0001f44b\n"
        f"Here's what's scheduled:\n"
        f"\n"
        f"{SEP}\n"
        f"\n"
        f"{blocks}\n"
        f"\n"
        f"{SEP}\n"
        f"\n"
        f"Get your tools ready and travel safe. \U0001f527\U0001f697"
    )


def msg_plumber_morning(apts: list, plumber_name: str = "there") -> str:
    blocks = f"\n{SEP}\n\n".join(_apt_block(i + 1, a, show_date=False) for i, a in enumerate(apts))
    return (
        f"\u2600\ufe0f *TODAY'S SCHEDULE*\n"
        f"\n"
        f"Good morning {plumber_name} \U0001f44b\n"
        f"\n"
        f"{SEP}\n"
        f"\n"
        f"{blocks}\n"
        f"\n"
        f"{SEP}\n"
        f"\n"
        f"Have a productive day! \U0001f4aa\U0001f527"
    )


def msg_plumber_2hours(apt, plumber_name: str = "there") -> str:
    phone = _fmt_phone(getattr(apt, "phone_number", "") or "")
    name  = getattr(apt, "customer_name", "?") or "?"
    return (
        f"\u23f0 *UPCOMING JOB \u2013 2 HOURS*\n"
        f"\n"
        f"Hi {plumber_name} \U0001f44b\n"
        f"\n"
        f"Customer: {name}\n"
        f"\U0001f6e0 {_service(apt)}\n"
        f"\U0001f4cd {_area(apt)}\n"
        f"\u23f0 {_apt_time(apt)}\n"
        f"\U0001f4de {phone}\n"
        f"\n"
        f"Make sure you're on your way.\n"
        f"\n"
        f"Drive safe! \U0001f697\U0001f527"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# CONSOLE DISPLAY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

W = 62


def _banner(text: str) -> str:
    return f"\n{'=' * W}\n  {text}\n{'=' * W}"


def _section(icon: str, title: str) -> str:
    return f"\n+- {icon} {title}\n|"


def _row(label: str, value: str, indent: int = 4) -> str:
    pad = " " * indent
    return f"{pad}{label:<24} {value}"


def _bar(count: int, total: int, width: int = 20) -> str:
    filled = round((count / total) * width) if total else 0
    return chr(0x2588) * filled + chr(0x2591) * (width - filled)


def _ok(msg: str) -> str:
    return f"  OK  {msg}"


def _skip(msg: str) -> str:
    return f"  --  {msg}"


def _warn(msg: str) -> str:
    return f"  !!  {msg}"


def _info(msg: str) -> str:
    return f"  ..  {msg}"


def _preview(text: str, max_chars: int = 55) -> str:
    first_line = text.strip().split("\n")[0]
    snippet = first_line[:max_chars] + ("..." if len(first_line) > max_chars else "")
    return f'       >> "{snippet}"'


# ═══════════════════════════════════════════════════════════════════════════════
# TIME HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def is_in_window(now_local, target_hour: int, target_minute: int = 0) -> bool:
    target = now_local.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    return abs((now_local - target).total_seconds()) <= WINDOW_MINUTES * 60


def is_2h_window(appt_utc_dt, now_utc) -> bool:
    diff = (appt_utc_dt - now_utc).total_seconds()
    return (1 * 3600 + 55 * 60) <= diff <= (2 * 3600 + 5 * 60)


def appt_utc(apt):
    import datetime
    import pytz
    d = getattr(apt, "appointment_date", None)
    t = getattr(apt, "appointment_time", None)
    if not d or not t:
        return None
    if isinstance(d, str):
        d = date.fromisoformat(d)
    if isinstance(t, str):
        parts = t.split(":")
        t = dt_time(int(parts[0]), int(parts[1]))
    cat = pytz.timezone(TIMEZONE_NAME)
    return cat.localize(datetime.datetime.combine(d, t)).astimezone(pytz.utc)


# ═══════════════════════════════════════════════════════════════════════════════
# DUPLICATE PREVENTION
# ═══════════════════════════════════════════════════════════════════════════════

def _key(apt_id: int, rtype: str) -> str:
    return f"reminder_sent_{rtype}_{apt_id}"


def already_sent(apt, rtype: str) -> bool:
    notes = getattr(apt, "internal_notes", "") or ""
    return _key(apt.id, rtype) in notes


def mark_sent(apt, rtype: str):
    k = _key(apt.id, rtype)
    existing = getattr(apt, "internal_notes", "") or ""
    apt.internal_notes = f"{existing}\n[{k}]".strip()
    apt.save(update_fields=["internal_notes"])


# ═══════════════════════════════════════════════════════════════════════════════
# WHATSAPP SENDER
# ═══════════════════════════════════════════════════════════════════════════════

def send_wa(phone: str, message: str) -> bool:
    try:
        from bot.whatsapp_cloud_api import whatsapp_api
        clean = _fmt_phone(phone)
        whatsapp_api.send_text_message(clean, message)
        return True
    except Exception as e:
        logger.error(f"WhatsApp send failed to {phone}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# MANAGEMENT COMMAND
# ═══════════════════════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = "Send appointment reminders to leads and plumber"

    def handle(self, *args, **options):
        try:
            from bot.models import Appointment
        except ImportError:
            self.stderr.write("Could not import Appointment model.")
            return

        import pytz
        cat       = pytz.timezone(TIMEZONE_NAME)
        now_utc   = timezone.now()
        now_local = now_utc.astimezone(cat)
        today     = now_local.date()

        # ── Banner ─────────────────────────────────────────────────────────────
        self.stdout.write(_banner("REMINDER DISPATCHER"))

        day_names = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        months    = ["January","February","March","April","May","June",
                     "July","August","September","October","November","December"]
        day_str   = f"{day_names[now_local.weekday()]}, {now_local.day} {months[now_local.month-1]} {now_local.year}"

        self.stdout.write("")
        self.stdout.write(_row("  Local time:", f"{day_str}  |  {now_local.strftime('%H:%M')} CAT"))
        self.stdout.write(_row("  UTC time:",   now_utc.strftime("%Y-%m-%d %H:%M UTC")))

        # ── Fetch appointments ─────────────────────────────────────────────────
        active = list(Appointment.objects.filter(
            Q(status__in=["confirmed", "scheduled", "booked", "pending"]),
            Q(appointment_date__gte=today),
            appointment_date__isnull=False,
            appointment_time__isnull=False,
        ).order_by("appointment_date", "appointment_time"))

        # ── Overview ───────────────────────────────────────────────────────────
        self.stdout.write(_section("OVERVIEW", "APPOINTMENT SUMMARY"))
        self.stdout.write("")

        by_date = defaultdict(list)
        for a in active:
            by_date[a.appointment_date].append(a)

        first_d = min(by_date.keys()) if by_date else None
        last_d  = max(by_date.keys()) if by_date else None

        self.stdout.write(_row("  Total active:", f"{len(active)} appointments"))
        self.stdout.write(_row("  Days covered:", str(len(by_date))))
        self.stdout.write(_row("  First:", str(first_d) if first_d else "none"))
        self.stdout.write(_row("  Last:",  str(last_d)  if last_d  else "none"))

        if by_date:
            self.stdout.write("")
            self.stdout.write("  Daily breakdown:")
            import datetime as _dt
            max_count = max(len(v) for v in by_date.values())
            short_days   = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
            short_months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
            for d, apts_on_day in sorted(by_date.items()):
                if isinstance(d, str):
                    d = _dt.date.fromisoformat(d)
                label = f"{short_days[d.weekday()]} {d.day} {short_months[d.month-1]}:"
                bar   = chr(0x2588) * len(apts_on_day) + " " * (max_count - len(apts_on_day))
                self.stdout.write(f"    {label:<14} {len(apts_on_day)}  [{bar}]")

        # ─────────────────────────────────────────────────────────────────────
        # LEAD REMINDERS
        # ─────────────────────────────────────────────────────────────────────
        self.stdout.write(_section("LEADS", "CUSTOMER REMINDERS"))
        self.stdout.write("")

        lead_sent = lead_skipped = 0
        plumber_contact = f"+{PLUMBER_PHONE}" if PLUMBER_PHONE else "+263610318200"

        LEAD_CHECKS = [
            (2, 18, "lead_2days",   msg_lead_2days,   "2 Days Before  [6:00 PM]"),
            (1, 18, "lead_1day",    msg_lead_1day,    "1 Day Before   [6:00 PM]"),
            (0,  7, "lead_morning", msg_lead_morning, "Morning Of     [7:00 AM]"),
        ]

        for apt in active:
            apt_u = appt_utc(apt)
            if apt_u is None:
                continue
            apt_loc   = apt_u.astimezone(cat)
            days_away = (apt_loc.date() - today).days
            phone     = _fmt_phone(getattr(apt, "phone_number", "") or "")
            name      = getattr(apt, "customer_name", "?") or "?"
            apt_label = f"{name} (+{phone})  |  {apt_loc.strftime('%Y-%m-%d @ %H:%M')}"

            for days, hour, rtype, builder, label in LEAD_CHECKS:
                if days_away == days and is_in_window(now_local, hour):
                    if already_sent(apt, rtype):
                        lead_skipped += 1
                        self.stdout.write(_skip(f"{label} -> {apt_label}"))
                    else:
                        msg = builder(apt, plumber_contact)
                        if send_wa(phone, msg):
                            mark_sent(apt, rtype)
                            lead_sent += 1
                            self.stdout.write(_ok(f"{label} -> {apt_label}"))
                            self.stdout.write(_preview(msg))
                        else:
                            self.stdout.write(_warn(f"{label} -> FAILED for {apt_label}"))

            if is_2h_window(apt_u, now_utc):
                rtype = "lead_2hours"
                if already_sent(apt, rtype):
                    lead_skipped += 1
                    self.stdout.write(_skip(f"2 Hours Before         -> {apt_label}"))
                else:
                    msg = msg_lead_2hours(apt, plumber_contact)
                    if send_wa(phone, msg):
                        mark_sent(apt, rtype)
                        lead_sent += 1
                        self.stdout.write(_ok(f"2 Hours Before         -> {apt_label}"))
                        self.stdout.write(_preview(msg))
                    else:
                        self.stdout.write(_warn(f"2 Hours Before -> FAILED for {apt_label}"))

        if lead_sent == 0 and lead_skipped == 0:
            self.stdout.write(_info("No lead reminders due at this time"))

        self.stdout.write("")
        self.stdout.write(f"  {'-' * 56}")
        self.stdout.write(f"  Lead Summary:")
        self.stdout.write(_row("    Sent:",    str(lead_sent)))
        self.stdout.write(_row("    Skipped:", str(lead_skipped)))
        self.stdout.write(_row("    Total:",   str(lead_sent + lead_skipped)))

        # ─────────────────────────────────────────────────────────────────────
        # PLUMBER REMINDERS
        # ─────────────────────────────────────────────────────────────────────
        self.stdout.write(_section("PLUMBER", "PLUMBER REMINDERS"))
        self.stdout.write("")

        plumber_sent = 0

        if not PLUMBER_PHONE:
            self.stdout.write(_warn("PLUMBER_PHONE_NUMBER not set -- skipping"))
        else:
            self.stdout.write(_row("  Recipient:", f"{PLUMBER_NAME}  |  {PLUMBER_PHONE}"))
            self.stdout.write("")

            import datetime as _dt
            week_num  = now_local.isocalendar()[1]
            is_sunday = now_local.weekday() == 6

            # Sunday weekly @ 18:00
            if is_sunday and is_in_window(now_local, 18):
                rtype  = f"plumber_weekly_{week_num}"
                marker = active[0] if active else None
                if marker and already_sent(marker, rtype):
                    self.stdout.write(_skip(f"Weekly Overview (Week {week_num})  [already sent]"))
                elif active:
                    msg = msg_plumber_weekly(active, PLUMBER_NAME)
                    if send_wa(PLUMBER_PHONE, msg):
                        if marker:
                            mark_sent(marker, rtype)
                        plumber_sent += 1
                        self.stdout.write(_ok(f"Weekly Overview (Week {week_num})  |  {len(active)} appointments"))
                        self.stdout.write(_preview(msg))
                    else:
                        self.stdout.write(_warn("Weekly Overview -- SEND FAILED"))
                else:
                    self.stdout.write(_info("No upcoming appointments -- weekly summary skipped"))

            # Tomorrow's appointments @ 20:00
            if is_in_window(now_local, 20):
                tomorrow      = today + timedelta(days=1)
                tomorrow_apts = [a for a in active if a.appointment_date == tomorrow]
                if tomorrow_apts:
                    rtype = f"plumber_nextday_{tomorrow.isoformat()}"
                    if already_sent(tomorrow_apts[0], rtype):
                        self.stdout.write(_skip(f"Tomorrow's Jobs  |  {len(tomorrow_apts)} appointments  [already sent]"))
                    else:
                        msg = msg_plumber_next_day(tomorrow_apts, PLUMBER_NAME)
                        if send_wa(PLUMBER_PHONE, msg):
                            mark_sent(tomorrow_apts[0], rtype)
                            plumber_sent += 1
                            self.stdout.write(_ok(f"Tomorrow's Jobs  |  {len(tomorrow_apts)} appointments"))
                            self.stdout.write(_preview(msg))
                        else:
                            self.stdout.write(_warn("Tomorrow's Jobs -- SEND FAILED"))
                else:
                    self.stdout.write(_info("No appointments tomorrow -- evening briefing skipped"))

            # Morning @ 07:00
            if is_in_window(now_local, 7):
                today_apts = [a for a in active if a.appointment_date == today]
                if today_apts:
                    rtype = f"plumber_morning_{today.isoformat()}"
                    if already_sent(today_apts[0], rtype):
                        self.stdout.write(_skip(f"Morning Briefing  |  {len(today_apts)} appointments  [already sent]"))
                    else:
                        msg = msg_plumber_morning(today_apts, PLUMBER_NAME)
                        if send_wa(PLUMBER_PHONE, msg):
                            mark_sent(today_apts[0], rtype)
                            plumber_sent += 1
                            self.stdout.write(_ok(f"Morning Briefing  |  {len(today_apts)} appointments"))
                            self.stdout.write(_preview(msg))
                        else:
                            self.stdout.write(_warn("Morning Briefing -- SEND FAILED"))
                else:
                    self.stdout.write(_info("No appointments today -- morning briefing skipped"))

            # 2-hour alerts
            for apt in active:
                apt_u = appt_utc(apt)
                if apt_u is None:
                    continue
                if is_2h_window(apt_u, now_utc):
                    rtype = "plumber_2hours"
                    name  = getattr(apt, "customer_name", "?") or "?"
                    if already_sent(apt, rtype):
                        self.stdout.write(_skip(f"2-Hour Alert -> {name}  [already sent]"))
                    else:
                        msg = msg_plumber_2hours(apt, PLUMBER_NAME)
                        if send_wa(PLUMBER_PHONE, msg):
                            mark_sent(apt, rtype)
                            plumber_sent += 1
                            self.stdout.write(_ok(f"2-Hour Alert -> {name}"))
                            self.stdout.write(_preview(msg))
                        else:
                            self.stdout.write(_warn(f"2-Hour Alert -> FAILED for {name}"))

        # ─────────────────────────────────────────────────────────────────────
        # FINAL TALLY
        # ─────────────────────────────────────────────────────────────────────
        total_sent = lead_sent + plumber_sent
        total_ops  = total_sent + lead_skipped
        rate       = (total_sent / total_ops * 100) if total_ops else 0.0

        self.stdout.write(f"\n{'=' * W}")
        self.stdout.write(f"  Final Tally:")
        self.stdout.write(_row("    Lead reminders sent:",    str(lead_sent)))
        self.stdout.write(_row("    Plumber reminders sent:", str(plumber_sent)))
        self.stdout.write(_row("    TOTAL:",                  str(total_sent)))
        self.stdout.write("")
        self.stdout.write(f"  Success Rate: {rate:.1f}%")
        bar_str = _bar(total_sent, total_ops) if total_ops else chr(0x2591) * 20
        self.stdout.write(f"  [{bar_str}]")
        self.stdout.write("")
        self.stdout.write("  Next run scheduled in ~15 minutes")
        self.stdout.write(f"{'=' * W}\n")