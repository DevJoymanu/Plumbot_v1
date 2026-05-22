"""
Django Management Command: process_inbound_emails
==================================================
Polls a Gmail inbox via IMAP every 5 minutes (run via Railway Scheduler):

    python manage.py process_inbound_emails

Cron:
    */5 * * * * cd /app && python manage.py process_inbound_emails >> /var/log/inbound_emails.log 2>&1

Required environment variables:
    IMAP_EMAIL      e.g.  team@homebaseplumbers.co.zw  (or a Gmail address)
    IMAP_PASSWORD   Gmail App Password (not your regular password)
    IMAP_HOST       imap.gmail.com  (default)
    IMAP_PORT       993             (default)

Flow per incoming email:
  1. Parse subject for [APT-XXX] → match Appointment in DB
  2. Strip quoted text → extract new customer message only
  3. DeepSeek classifies intent: reschedule / book / cancel / confirm / query / other
  4. Handle intent — update DB, send reply email, notify plumber if needed
  5. Mark email as read (\\Seen)

New emails with no [APT-XXX] tag are logged and skipped (manual handling).
"""

import email
import imaplib
import logging
import os
import re
from datetime import timedelta
from email.header import decode_header
from email.utils import parseaddr

import pytz
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from openai import OpenAI

logger = logging.getLogger(__name__)

_SAST        = pytz.timezone("Africa/Johannesburg")
_APT_TAG_RE  = re.compile(r'\[APT-(\d+)\]', re.IGNORECASE)
_EMAIL_FROM  = os.environ.get("IMAP_EMAIL", "")
_IMAP_HOST   = os.environ.get("IMAP_HOST", "imap.gmail.com")
_IMAP_PORT   = int(os.environ.get("IMAP_PORT", 993))
_IMAP_PASS   = os.environ.get("IMAP_PASSWORD", "")

_deepseek = (
    OpenAI(
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        base_url="https://api.deepseek.com/v1",
    )
    if os.environ.get("DEEPSEEK_API_KEY")
    else None
)


# ── IMAP helpers ──────────────────────────────────────────────────────────────

def _connect():
    """Open an authenticated IMAP SSL connection. Returns imap object or None."""
    if not _EMAIL_FROM or not _IMAP_PASS:
        logger.error("IMAP_EMAIL or IMAP_PASSWORD not set — cannot connect")
        return None
    try:
        imap = imaplib.IMAP4_SSL(_IMAP_HOST, _IMAP_PORT)
        imap.login(_EMAIL_FROM, _IMAP_PASS)
        return imap
    except Exception as e:
        logger.exception("IMAP connection failed: %s", e)
        return None


def _fetch_unseen(imap):
    """Return list of (uid_bytes, raw_message_bytes) for all UNSEEN emails."""
    imap.select("INBOX")
    status, data = imap.uid("search", None, "UNSEEN")
    if status != "OK" or not data[0]:
        return []
    results = []
    for uid in data[0].split():
        s, msg_data = imap.uid("fetch", uid, "(RFC822)")
        if s == "OK" and msg_data and msg_data[0]:
            results.append((uid, msg_data[0][1]))
    return results


def _mark_seen(imap, uid):
    imap.uid("store", uid, "+FLAGS", "\\Seen")


def _decode_header_value(value):
    parts = decode_header(value or "")
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _extract_apt_id(subject: str):
    m = _APT_TAG_RE.search(subject or "")
    return int(m.group(1)) if m else None


def _get_plain_body(msg):
    """Extract plain-text body, preferring text/plain over text/html."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                charset = part.get_content_charset() or "utf-8"
                body = part.get_payload(decode=True).decode(charset, errors="replace")
                break
        if not body:
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/html":
                    charset = part.get_content_charset() or "utf-8"
                    raw = part.get_payload(decode=True).decode(charset, errors="replace")
                    body = re.sub(r'<[^>]+>', ' ', raw)
                    break
    else:
        charset = msg.get_content_charset() or "utf-8"
        body = msg.get_payload(decode=True).decode(charset, errors="replace")
    return body.strip()


# Strip common email reply artifacts (quoted text, signatures)
_QUOTE_PATTERNS = [
    re.compile(r'On .{5,80}wrote:', re.DOTALL),
    re.compile(r'-{3,}\s*Original Message\s*-{3,}', re.IGNORECASE),
    re.compile(r'From:\s.+?Sent:\s', re.DOTALL),
    re.compile(r'>{1,}.*', re.MULTILINE),
]


def _strip_quoted(text: str) -> str:
    for pattern in _QUOTE_PATTERNS:
        text = pattern.split(text)[0]
    lines = [l for l in text.splitlines() if not l.strip().startswith('>')]
    return "\n".join(lines).strip()


# ── Intent detection ──────────────────────────────────────────────────────────

_INTENT_SYSTEM = """You are an email intent classifier for a plumbing company.

Classify the customer email into ONE of:
  reschedule  — customer wants to change their appointment date/time
  book        — customer wants to book a new appointment (no existing confirmed booking)
  cancel      — customer wants to cancel their appointment
  confirm     — customer is confirming their existing appointment
  query       — customer has a question (pricing, service, location, etc.)
  other       — none of the above

Also extract:
  date  — the preferred date/time mentioned (ISO 8601 if possible, else descriptive string, or null)

Respond with ONLY valid JSON:
{"intent": "reschedule", "date": "2025-05-10T10:00:00" | "next Thursday morning" | null}"""


def _classify_intent(body: str, appointment=None) -> dict:
    """Use DeepSeek to classify the email intent."""
    if not _deepseek:
        return {"intent": "other", "date": None}

    apt_context = ""
    if appointment and appointment.scheduled_datetime:
        dt = appointment.scheduled_datetime.astimezone(_SAST)
        apt_context = f"\nExisting appointment: {dt.strftime('%A %d %B %Y at %H:%M')}"

    try:
        resp = _deepseek.chat.completions.create(
            model=settings.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": _INTENT_SYSTEM},
                {"role": "user",   "content": f"{apt_context}\n\nCustomer email:\n{body[:800]}"},
            ],
            temperature=0.0,
            max_tokens=80,
        )
        import json
        raw = resp.choices[0].message.content.strip()
        return json.loads(raw)
    except Exception as e:
        logger.warning("Intent classification failed: %s", e)
        return {"intent": "other", "date": None}


# ── Date parsing ──────────────────────────────────────────────────────────────

def _parse_date_hint(date_hint: str, appointment=None):
    """
    Try to parse a date hint string into a timezone-aware datetime.
    Returns datetime or None.
    """
    if not date_hint:
        return None
    from datetime import datetime
    # Try ISO format first
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_hint[:19], fmt)
            return _SAST.localize(dt)
        except ValueError:
            pass
    return None  # descriptive string — caller will ask for clarification


# ── Reply email builders ──────────────────────────────────────────────────────

def _months():
    return ["January","February","March","April","May","June",
            "July","August","September","October","November","December"]


def _fmt_dt(dt):
    d = dt.date()
    return f"{d.day} {_months()[d.month-1]} {d.year} at {dt.strftime('%H:%M')}"


def _send_reply(apt, subject, html_body):
    """Send HTML email to the customer and tag with APT id."""
    from bot.customer_emails import _send, _wrap
    html = _wrap(html_body)
    return _send(apt, subject, html)


# ── AI reply generator ────────────────────────────────────────────────────────

def _generate_email_reply(body: str, apt=None) -> str:
    """Use DeepSeek to answer a customer email query directly."""
    if not _deepseek:
        return "Thank you for your message. We will be in touch shortly via WhatsApp."

    service = (getattr(apt, "project_type", "") or "").replace("_", " ").title()
    area    = getattr(apt, "customer_area", "") or ""

    system = (
        "You are a customer support agent for HomeBase Plumbers in Harare, Zimbabwe. "
        "The plumber's name is Takudzwa. "
        "Answer the customer's email in 2-4 sentences — directly and helpfully. "
        "Services: bathroom renovation, kitchen renovation, new plumbing installation, "
        "drain unblocking, pipe repair, geyser repair, toilet repair. "
        "Pricing: toilet from US$50 supply + US$20 install, shower cubicle US$130 + US$40 install, "
        "geyser US$80 + US$80 install, full bathroom from US$600. Site assessment is free. "
        "Hours: Sunday to Friday, 08:00 to 18:00. Based in Hatfield, Harare. "
        "Never use 'our' — say 'we' or 'the team'. "
        "Never use contractions — write 'we will' not 'we'll'. "
        "Write professionally but warmly. No bullet points."
    )
    if service:
        system += f" Customer service interest: {service}."
    if area:
        system += f" Customer area: {area}."

    try:
        resp = _deepseek.chat.completions.create(
            model=settings.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": body[:600]},
            ],
            temperature=0.3,
            max_tokens=150,
        )
        reply = resp.choices[0].message.content.strip()
        return reply if reply else "Thank you for your message. We will be in touch shortly via WhatsApp."
    except Exception as e:
        logger.warning("Email reply generation failed: %s", e)
        return "Thank you for your message. We will be in touch shortly via WhatsApp."


# ── Intent handlers ───────────────────────────────────────────────────────────

def _handle_reschedule(apt, date_hint, body, dry_run, stdout):
    """Customer wants to reschedule."""
    dt   = _parse_date_hint(date_hint)
    name = getattr(apt, "customer_name", "") or "there"

    if dt:
        if not dry_run:
            apt.scheduled_datetime = dt
            apt.save(update_fields=["scheduled_datetime"])

        body_html = (
            f'<p>Hi {name},</p>'
            f'<p>Done! Your appointment has been rescheduled to '
            f'<strong>{_fmt_dt(dt.astimezone(_SAST))}</strong>.</p>'
            '<p>If you need to make any further changes, just reply to this email.</p>'
            '<p><strong>HomeBase Plumbers</strong></p>'
        )
        if not dry_run:
            _send_reply(apt, f"✅ Appointment Rescheduled — {_fmt_dt(dt.astimezone(_SAST))}", body_html)
        stdout(f"    ✅ Rescheduled apt #{apt.pk} → {dt}")
    else:
        body_html = (
            f'<p>Hi {name},</p>'
            '<p>Happy to reschedule for you! Could you let me know the specific '
            '<strong>date and time</strong> that works best?</p>'
            '<p>We are available Sunday to Friday, 08:00 to 18:00.</p>'
            '<p><strong>HomeBase Plumbers</strong></p>'
        )
        if not dry_run:
            _send_reply(apt, "Reschedule Request — What Date Works for You?", body_html)
        stdout(f"    ℹ️  Reschedule — asked for specific date, apt #{apt.pk}")


def _handle_book(apt, date_hint, body, dry_run, stdout):
    """Customer (delayed lead) wants to book a new appointment."""
    name = getattr(apt, "customer_name", "") or "there"
    dt   = _parse_date_hint(date_hint)

    if dt:
        if not dry_run:
            apt.scheduled_datetime = dt
            apt.status             = "pending"
            apt.save(update_fields=["scheduled_datetime", "status"])

        body_html = (
            f'<p>Hi {name},</p>'
            f'<p>Great to hear from you! We have noted <strong>{_fmt_dt(dt.astimezone(_SAST))}</strong> '
            'as your preferred slot. The team will confirm availability shortly.</p>'
            '<p>If that date does not work, just reply with an alternative.</p>'
            '<p><strong>HomeBase Plumbers</strong></p>'
        )
        if not dry_run:
            _send_reply(apt, "Booking Request Received — We Will Confirm Shortly", body_html)
        stdout(f"    ✅ Booking requested apt #{apt.pk} → {dt}")
    else:
        body_html = (
            f'<p>Hi {name},</p>'
            '<p>Wonderful — we would love to get you booked in!</p>'
            '<p>Could you let me know your preferred <strong>date and time</strong>?</p>'
            '<p>We are available Sunday to Friday, 08:00 to 18:00.</p>'
            '<p><strong>HomeBase Plumbers</strong></p>'
        )
        if not dry_run:
            _send_reply(apt, "Let Us Get You Booked — What Day Works?", body_html)
        stdout(f"    ℹ️  Book request — asked for date, apt #{apt.pk}")


def _handle_cancel(apt, body, dry_run, stdout):
    """Customer wants to cancel."""
    name = getattr(apt, "customer_name", "") or "there"
    if not dry_run:
        apt.status = "cancelled"
        apt.save(update_fields=["status"])

    body_html = (
        f'<p>Hi {name},</p>'
        '<p>Your appointment has been <strong>cancelled</strong>. '
        'We are sorry to see you go!</p>'
        '<p>Whenever you are ready to rebook, just reply to this email or '
        'send us a WhatsApp message.</p>'
        '<p><strong>HomeBase Plumbers</strong></p>'
    )
    if not dry_run:
        _send_reply(apt, "Appointment Cancelled", body_html)
    stdout(f"    ❌ Cancelled apt #{apt.pk}")


def _handle_query(apt, body, dry_run, stdout):
    """Customer has a question — answer directly with DeepSeek."""
    name   = getattr(apt, "customer_name", "") or "there"
    answer = _generate_email_reply(body, apt)
    body_html = (
        f'<p>Hi {name},</p>'
        f'<p>{answer}</p>'
        '<p>If you have any other questions, feel free to reply or WhatsApp us directly.</p>'
        '<p><strong>HomeBase Plumbers</strong></p>'
    )
    if not dry_run:
        _send_reply(apt, "Re: Your Enquiry — HomeBase Plumbers", body_html)
    stdout(f"    ✅ Query answered directly for apt #{apt.pk}")


def _handle_other(apt, body, dry_run, stdout):
    """Unrecognised intent — generate a helpful reply directly."""
    name   = getattr(apt, "customer_name", "") or "there"
    answer = _generate_email_reply(body, apt)
    body_html = (
        f'<p>Hi {name},</p>'
        f'<p>{answer}</p>'
        '<p><strong>HomeBase Plumbers</strong></p>'
    )
    if not dry_run:
        _send_reply(apt, "Re: Your Message — HomeBase Plumbers", body_html)
    stdout(f"    ✅ Replied to unclassified email for apt #{apt.pk}")


# ── Main command ──────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = (
        "Poll Gmail inbox for customer email replies. "
        "Matches replies to appointments via [APT-XXX] subject tag, "
        "classifies intent with DeepSeek, and handles reschedule / book / cancel."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and classify emails without sending replies or updating DB.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        try:
            from bot.models import Appointment
        except ImportError:
            self.stderr.write("Could not import Appointment model.")
            return

        if dry_run:
            self.stdout.write(self.style.WARNING("🧪 DRY RUN — no replies sent, no DB changes\n"))

        self.stdout.write(
            f"\n{'=' * 60}\n"
            f"  INBOUND EMAIL PROCESSOR\n"
            f"{'=' * 60}\n"
        )

        if not _EMAIL_FROM or not _IMAP_PASS:
            self.stdout.write(
                self.style.WARNING(
                    "  IMAP_EMAIL or IMAP_PASSWORD not configured — skipping.\n"
                    "  Set these environment variables to enable inbound email processing."
                )
            )
            return

        imap = _connect()
        if not imap:
            self.stdout.write(self.style.ERROR("  Failed to connect to IMAP server."))
            return

        emails = _fetch_unseen(imap)
        self.stdout.write(f"  Unseen emails found: {len(emails)}\n")

        processed = skipped = errors = 0

        def out(msg):
            self.stdout.write(msg)

        for uid, raw in emails:
            try:
                msg     = email.message_from_bytes(raw)
                subject = _decode_header_value(msg.get("Subject", ""))
                sender  = parseaddr(msg.get("From", ""))[1]
                apt_id  = _extract_apt_id(subject)

                out(f"\n  ─ From: {sender} | Subject: {subject[:70]}")

                if not apt_id:
                    out(f"    SKIP  No [APT-XXX] tag in subject — manual handling required")
                    skipped += 1
                    _mark_seen(imap, uid)
                    continue

                try:
                    apt = Appointment.objects.get(pk=apt_id)
                except Appointment.DoesNotExist:
                    out(f"    SKIP  Appointment #{apt_id} not found in DB")
                    skipped += 1
                    _mark_seen(imap, uid)
                    continue

                body   = _get_plain_body(msg)
                clean  = _strip_quoted(body)

                if not clean:
                    out(f"    SKIP  Empty body after stripping quotes")
                    skipped += 1
                    _mark_seen(imap, uid)
                    continue

                out(f"    APT #{apt_id} | Customer: {apt.customer_name or sender}")
                out(f"    Body: {clean[:100]}{'…' if len(clean) > 100 else ''}")

                result     = _classify_intent(clean, apt)
                intent     = result.get("intent", "other")
                date_hint  = result.get("date")

                out(f"    Intent: {intent} | Date hint: {date_hint}")

                if intent == "reschedule":
                    _handle_reschedule(apt, date_hint, clean, dry_run, out)
                elif intent == "book":
                    _handle_book(apt, date_hint, clean, dry_run, out)
                elif intent == "cancel":
                    _handle_cancel(apt, clean, dry_run, out)
                elif intent == "confirm":
                    out(f"    ✅ Confirmation received — no action needed, apt #{apt_id}")
                elif intent == "query":
                    _handle_query(apt, clean, dry_run, out)
                else:
                    _handle_other(apt, clean, dry_run, out)

                _mark_seen(imap, uid)
                processed += 1

            except Exception as e:
                logger.exception("Error processing email uid=%s: %s", uid, e)
                out(self.style.ERROR(f"    ERROR processing email: {e}"))
                errors += 1

        try:
            imap.logout()
        except Exception:
            pass

        self.stdout.write(
            f"\n{'=' * 60}\n"
            f"  DONE  processed={processed}  skipped={skipped}  errors={errors}\n"
            f"{'=' * 60}\n"
        )
