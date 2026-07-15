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
  3. DeepSeek classifies structural intent (reschedule / cancel / book / confirm / other)
  4. Apply DB changes for structural intents (reschedule datetime, cancel status)
  5. Generate a full Plumbot-style reply using the same Hormozi framework as WhatsApp
  6. Append both messages to appointment.conversation_history
  7. Send HTML reply email with contact buttons
  8. Mark email as read (\\Seen)

New emails with no [APT-XXX] tag are logged and skipped (manual handling).
"""

import email
import html as _html
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
logger = logging.getLogger(__name__)

_SAST        = pytz.timezone("Africa/Johannesburg")
_APT_TAG_RE  = re.compile(r'\[APT-(\d+)\]', re.IGNORECASE)
_EMAIL_FROM  = os.environ.get("IMAP_EMAIL", "")
_IMAP_HOST   = os.environ.get("IMAP_HOST", "imap.gmail.com")
_IMAP_PORT   = int(os.environ.get("IMAP_PORT", 993))
_IMAP_PASS   = os.environ.get("IMAP_PASSWORD", "")


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


def _extract_apt_id(subject: str, msg=None):
    """
    Extract appointment PK.
    Primary:  In-Reply-To / References header — the APT ID is encoded in the
              Message-ID we set when sending (e.g. <apt-375.1748123456@...>).
    Fallback: [APT-XXX] tag in subject — for legacy emails sent before the
              Message-ID approach was deployed.
    """
    if msg is not None:
        for header in ("In-Reply-To", "References"):
            val = msg.get(header, "") or ""
            m = re.search(r'<apt-(\d+)\.', val, re.IGNORECASE)
            if m:
                return int(m.group(1))
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
  reschedule      — customer wants to change their appointment date/time
  book            — customer wants to book a new appointment (no existing confirmed booking)
  cancel          — customer wants to cancel their appointment
  confirm         — customer is confirming their existing appointment
  query           — customer has a question (pricing, service, location, etc.)
  acknowledgement — customer is only acknowledging receipt, saying thanks, or politely closing the thread with NO question and NO new information. Examples: "thanks", "thank you", "thx", "thnks", "ok", "okay", "kk", "alright", "noted", "cool", "got it", "👍", "received", or Shona equivalents like "tatenda", "maita", "zvakanaka", "sawa". Use this intent even if the word is misspelled or abbreviated. If they say thanks AND ask something or provide new info, prefer 'query' or the matching intent instead.
  other           — none of the above

Also extract:
  date  — the preferred date/time mentioned (ISO 8601 if possible, else descriptive string, or null)

Respond with ONLY valid JSON:
{"intent": "reschedule", "date": "2025-05-10T10:00:00" | "next Thursday morning" | null}"""


def _classify_intent(body: str, appointment=None) -> dict:
    """Classify email intent using DeepSeek."""
    from bot.services.clients import deepseek_call
    import json

    apt_context = ""
    if appointment and appointment.scheduled_datetime:
        dt = appointment.scheduled_datetime.astimezone(_SAST)
        apt_context = f"\nExisting appointment: {dt.strftime('%A %d %B %Y at %H:%M')}"

    try:
        raw = deepseek_call(
            messages=[
                {"role": "system", "content": _INTENT_SYSTEM},
                {"role": "user",   "content": f"{apt_context}\n\nCustomer email:\n{body[:800]}"},
            ],
            temperature=0.0,
            max_tokens=120,
            json_response=True,
        )
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
    html = _wrap(html_body, apt)
    return _send(apt, subject, html)


# ── Plumbot email engine ──────────────────────────────────────────────────────

_EMAIL_PLUMBOT_SYSTEM = """You are Plumbot, the sales and scheduling assistant for {business_name} in Zimbabwe. You are communicating via EMAIL — write in clean, professional email style. No markdown, no asterisks, no emojis.

Before every reply, reason through these steps internally:
1. Intent — What is the customer asking or signalling? Look beyond the literal words.
2. Stage — Which qualification stage are they in: value, price, qualification, or close?
3. History — What has already been discussed? Never repeat a question already answered.
4. Commitment signals — Are they ready to book? If yes, move to close immediately.
5. Exit signals — Are they stepping back? Acknowledge warmly and leave the door open.

Qualification framework (Hormozi order):
- Value: lead with what we offer and why it matters
- Price: be upfront about costs before deep qualification
- Qualification: service type, project detail, area, preferred timing
- Close: offer the free on-site assessment as a presumptive close

Pricing reference (starting rates):
{pricing_guide}
Free on-site assessment available — no obligation.
{hours_location_line}

Reply rules:
- Write in the same language the customer used (English or Shona)
- Keep replies concise — 2 to 5 sentences for most messages
- Use presumptive framing — offer choices, not yes/no questions
- Never repitch the site visit to a customer who has already agreed to one
- When offering time slots, always use specific times — "9am or 2pm" not "morning or afternoon"
- Never ask for a property address — ask for area or neighbourhood instead (e.g. "Which area are you in?")
- If the customer volunteers their full address without being asked, accept it naturally and move on — never ask for it
"""
# (The per-tenant sign-off instruction is appended at call time —
# _generate_plumbot_email_reply — from the tenant profile.)


def _text_to_html(text: str) -> str:
    """Convert plain-text paragraphs to email-safe HTML."""
    paragraphs = text.strip().split("\n\n")
    parts = []
    for para in paragraphs:
        para = para.strip()
        if para:
            escaped = _html.escape(para).replace("\n", "<br>")
            parts.append(f'<p style="margin:0 0 14px;">{escaped}</p>')
    return "\n".join(parts)


def _build_email_html(reply_text: str, apt=None) -> str:
    """Wrap AI plain-text reply in email HTML with contact buttons."""
    from bot.customer_emails import _contact_buttons

    # Strip any WhatsApp markdown the AI may have included
    clean = reply_text.strip()
    for ch in ("**", "__", "*"):
        clean = clean.replace(ch, "")

    buttons = _contact_buttons(apt) if apt is not None else ''
    return _text_to_html(clean) + ("\n" + buttons if buttons else "")


def _reply_subject(apt, intent: str) -> str:
    """Build a personal reply subject line (APT tag appended by _send)."""
    name = getattr(apt, "customer_name", "") or ""
    svc  = (getattr(apt, "project_type", "") or "").replace("_", " ").title()
    suffix = f", {name}" if name else ""
    if intent == "reschedule":
        return f"Re: Your reschedule request{suffix}"
    if intent == "cancel":
        return f"Re: Cancellation confirmed{suffix}"
    if intent == "acknowledgement":
        return f"Re: Your message{suffix}"
    if svc:
        return f"Re: {svc}{suffix}"
    return f"Re: Your message{suffix}"


def _build_acknowledgement_reply(apt) -> tuple[str, str]:
    """
    Short, warm acknowledgement for a one-word reply ('thanks', 'ok', etc.).
    Returns (plain_text_for_history, html_body) — no value stack, no CTA
    buttons, no qualifying question. Mirrors how a human would handle a
    polite thread-closer.
    """
    name = getattr(apt, "customer_name", "") or ""
    hi   = f"Hi {name}" if name else "Hi there"
    from bot.customer_emails import _business_name, _from_name
    plain = (
        f"{hi},\n\n"
        "Got it — thanks. Talk soon.\n\n"
        f"{_from_name(apt)}\n{_business_name(apt)}"
    )
    html = _text_to_html(plain)
    return plain, html


_SIGNATURE_LINES = frozenset({
    "takudzwa", "takudzwa,", "homebase plumbers", "homebase plumbers.",
    "homebase plumbers,", "takudzwa | homebase plumbers",
})

_PRICING_KEYWORDS = ("us$", "usd", "supply", "install", "from us", "free assessment", "site visit")

# Maps intent strings (used by WhatsApp bot) to keywords that indicate pricing
# was discussed in the email reply for that specific product.
# Order matters: more specific entries must come before broader ones (e.g.
# standalone_tub before tub_sales so "freestanding tub" doesn't only hit tub_sales).
_PRICED_INTENT_KEYWORDS = [
    ("standalone_tub",   ("freestanding", "free-standing", "free standing", "standalone")),
    ("tub_sales",        ("bathtub", "standard tub", " tub ")),
    ("geyser_repair",    ("geyser repair",)),
    ("geyser",           ("geyser",)),
    ("shower_cubicle",   ("shower cubicle",)),
    ("vanity",           ("vanity", "vanities")),
    ("toilet_repair",    ("toilet repair",)),
    ("toilet",           ("toilet",)),
    ("chamber",          ("side chamber", " chamber")),
    ("drain_unblocking", ("drain unblock", "unblocking", "blocked drain")),
    ("pipe_repair",      ("pipe repair", "burst pipe", "leaking pipe")),
]


def _detect_priced_intents(reply_text: str) -> list[str]:
    """
    Return a list of pricing intent strings covered in this email reply.
    Only marks an intent if the reply contains BOTH a price figure (US$ / USD)
    AND a product keyword — so a general question reply without prices is ignored.
    """
    low = reply_text.lower()
    if "us$" not in low and "usd" not in low and "$" not in low:
        return []
    found = []
    for intent, keywords in _PRICED_INTENT_KEYWORDS:
        if any(kw in low for kw in keywords):
            found.append(intent)
    return found


def _strip_signature_for_history(text: str) -> str:
    """Remove email sign-off lines before saving to conversation_history."""
    lines = text.strip().split("\n")
    while lines and lines[-1].strip().lower() in _SIGNATURE_LINES:
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def _reply_discusses_pricing(reply_text: str) -> bool:
    low = reply_text.lower()
    return any(k in low for k in _PRICING_KEYWORDS)


_COMMITMENT_INTENTS = frozenset({"book", "reschedule", "confirm"})

_ACK_MARKER = "[ACK_REPLIED]"


def _ack_already_replied(apt) -> bool:
    """True if we've already sent an acknowledgement reply in this thread."""
    return _ACK_MARKER in (apt.internal_notes or "")


def _set_ack_replied(apt) -> None:
    """Mark that we've sent an ack reply, so subsequent acks stay silent."""
    notes = (apt.internal_notes or "").strip()
    if _ACK_MARKER in notes:
        return
    apt.internal_notes = f"{notes}\n{_ACK_MARKER}".strip()
    apt.save(update_fields=["internal_notes"])


def _clear_ack_replied(apt) -> None:
    """Clear the ack marker when the customer returns with substantive engagement."""
    notes = apt.internal_notes or ""
    if _ACK_MARKER not in notes:
        return
    apt.internal_notes = re.sub(r"\[ACK_REPLIED\]\n?", "", notes).strip()
    apt.save(update_fields=["internal_notes"])


def _sync_state_after_email(apt, reply_text: str, dry_run: bool, intent: str = "other") -> None:
    """
    Keep WhatsApp-specific state flags in sync after an email exchange so the
    WhatsApp bot does not repeat things already covered over email.

    - Clears is_delayed: ONLY on commitment intents (book / reschedule / confirm).
      A bare 'thanks', a question ('query'), or 'other' replies leave the
      delayed state intact — research and acknowledgement are not commitment,
      and pulling a customer out of their stated timeline disengages them.
    - Sets pricing_overview_sent: if reply contained any pricing.
    - Updates sent_pricing_intents: records every specific product that was priced.
    """
    if dry_run:
        return

    # Customer came back with substantive engagement → unmute future acks.
    if intent != "acknowledgement":
        _clear_ack_replied(apt)

    dirty = []

    if apt.is_delayed and intent in _COMMITMENT_INTENTS:
        apt.clear_delayed(save=False)
        dirty.append("is_delayed")

    if _reply_discusses_pricing(reply_text):
        if not apt.pricing_overview_sent:
            apt.pricing_overview_sent = True
            dirty.append("pricing_overview_sent")

        priced = _detect_priced_intents(reply_text)
        if priced:
            current = list(apt.sent_pricing_intents or [])
            new     = [i for i in priced if i not in current]
            if new:
                apt.sent_pricing_intents = current + new
                dirty.append("sent_pricing_intents")

    if dirty:
        apt.save(update_fields=dirty)


def _generate_plumbot_email_reply(
    customer_message: str, apt=None, context_note: str = ""
) -> str:
    """
    Generate a full Plumbot-style reply for an email using the Hormozi framework.
    Passes the full conversation history to DeepSeek for context continuity.
    context_note: system-side note about a DB action just taken (e.g. reschedule confirmed).
    """
    from bot.services.clients import deepseek_call

    service = (getattr(apt, "project_type", "") or "").replace("_", " ").title()
    area    = getattr(apt, "customer_area", "") or ""
    name    = getattr(apt, "customer_name", "") or ""
    status  = getattr(apt, "status", "") or ""

    from bot.customer_emails import _business_name, _from_name
    from bot.pricing_copy import build_prompt_pricing_guide
    from bot.tenant_config import get_config
    _tenant = getattr(apt, 'tenant', None)
    _cfg = get_config(_tenant)
    _hours = _cfg.hours_sentence()
    _loc = _cfg.location_short()
    _hl_bits = []
    if _hours:
        _hl_bits.append(f"Hours: {_hours}.")
    if _loc:
        _hl_bits.append(f"Based in {_loc}.")
    _signature = f"{_from_name(apt)}\\n{_business_name(apt)}" if apt is not None else "The team"
    system = _EMAIL_PLUMBOT_SYSTEM.format(
        business_name=_business_name(apt) if apt is not None else "the plumbing team",
        pricing_guide=build_prompt_pricing_guide(_cfg).replace("\n        ", "\n"),
        hours_location_line=" ".join(_hl_bits),
    ) + f"\n- Sign every reply exactly as: {_signature}"
    if service:
        system += f"\nCustomer service interest: {service}."
    if area:
        system += f"\nCustomer area: {area}."
    if name:
        system += f"\nCustomer name: {name}."
    if status == "confirmed":
        system += "\nThis appointment is already confirmed."
    if context_note:
        system += f"\n[ACTION TAKEN: {context_note}]"

    # Build message list from conversation history (last 14 messages for context)
    history  = getattr(apt, "conversation_history", None) or []
    messages = [{"role": "system", "content": system}]
    for msg in history[-14:]:
        role    = msg.get("role", "user")
        content = (msg.get("content") or "").strip()
        if content and not content.startswith("["):
            messages.append({
                "role": role if role in ("user", "assistant") else "user",
                "content": content,
            })
    messages.append({"role": "user", "content": customer_message[:1000]})

    try:
        reply = deepseek_call(
            messages=messages,
            temperature=0.4,
            max_tokens=350,
        )
        return reply.strip()
    except Exception as e:
        logger.warning("Plumbot email reply generation failed: %s", e)
        hi = f"Hi {name},\n\n" if name else ""
        from bot.customer_emails import _business_name, _from_name
        _sig = f"{_from_name(apt)}\n{_business_name(apt)}" if apt is not None else "The team"
        return (
            f"{hi}Thank you for your message. "
            "We will get back to you shortly.\n\n"
            f"{_sig}"
        )


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
                apt_id  = _extract_apt_id(subject, msg)

                out(f"\n  ─ From: {sender} | Subject: {subject[:70]}")

                if not apt_id:
                    out(f"    SKIP  No appointment reference found — manual handling required")
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

                # If the appointment has no email on record (e.g. lead came via
                # WhatsApp), capture it from the sender so we can reply.
                if not apt.customer_email and sender and "@" in sender:
                    apt.customer_email = sender
                    apt.save(update_fields=["customer_email"])
                    out(f"    📧 Saved customer email from sender: {sender}")

                body   = _get_plain_body(msg)
                clean  = _strip_quoted(body)

                if not clean:
                    out(f"    SKIP  Empty body after stripping quotes")
                    skipped += 1
                    _mark_seen(imap, uid)
                    continue

                out(f"    APT #{apt_id} | Customer: {apt.customer_name or sender}")
                out(f"    Body: {clean[:100]}{'…' if len(clean) > 100 else ''}")

                result    = _classify_intent(clean, apt)
                intent    = result.get("intent", "other")
                date_hint = result.get("date")

                out(f"    Intent: {intent} | Date hint: {date_hint}")

                # ── Structural DB actions ─────────────────────────────────────
                context_note = ""

                if intent == "reschedule":
                    dt = _parse_date_hint(date_hint)
                    if dt:
                        if not dry_run:
                            apt.scheduled_datetime = dt
                            apt.save(update_fields=["scheduled_datetime"])
                        context_note = (
                            f"Appointment rescheduled to {_fmt_dt(dt.astimezone(_SAST))}. "
                            "Confirm this clearly to the customer."
                        )
                        out(f"    ✅ Rescheduled apt #{apt.pk} → {dt}")
                    else:
                        context_note = (
                            "Customer wants to reschedule but did not give a specific date. "
                            "Ask for their preferred date and time."
                        )
                        out(f"    ℹ️  Reschedule — no date extracted, apt #{apt.pk}")

                elif intent == "cancel":
                    if not dry_run:
                        apt.status = "cancelled"
                        apt.save(update_fields=["status"])
                    context_note = (
                        "Appointment cancelled as the customer requested. "
                        "Confirm warmly and leave the door open for rebooking."
                    )
                    out(f"    ❌ Cancelled apt #{apt.pk}")

                elif intent == "book":
                    dt = _parse_date_hint(date_hint)
                    if dt:
                        if not dry_run:
                            apt.scheduled_datetime = dt
                            apt.status             = "pending"
                            apt.save(update_fields=["scheduled_datetime", "status"])
                        context_note = (
                            f"Booking request received for {_fmt_dt(dt.astimezone(_SAST))}. "
                            "Confirm receipt and note that the team will confirm availability."
                        )
                        out(f"    ✅ Booking requested apt #{apt.pk} → {dt}")
                    else:
                        context_note = (
                            "Customer wants to book but did not give a specific date or time. "
                            "Ask for their preferred date and time."
                        )
                        out(f"    ℹ️  Book intent — no date extracted, apt #{apt.pk}")

                elif intent == "confirm":
                    context_note = (
                        "Customer confirmed their appointment. "
                        "Acknowledge warmly and remind them of the appointment details."
                    )
                    out(f"    ✅ Confirmation received — apt #{apt.pk}")

                # ── Conversation history (customer turn) ──────────────────────
                if not dry_run:
                    apt.add_conversation_message("user", clean)

                # ── Acknowledgement-only reply (short path, no pitch) ─────────
                if intent == "acknowledgement":
                    # Reply once per thread. If we've already sent an ack reply,
                    # stay silent — replying to "ok" after we said "talk soon"
                    # makes the system feel automated and undoes the warmth.
                    if _ack_already_replied(apt):
                        out("    Acknowledgement (suppressed — already replied once in this thread)")
                        _mark_seen(imap, uid)
                        processed += 1
                        continue

                    plain_reply, html_body = _build_acknowledgement_reply(apt)
                    out(f"    Reply preview: {plain_reply[:80]}{'…' if len(plain_reply) > 80 else ''}")

                    if not dry_run:
                        history_reply = _strip_signature_for_history(plain_reply)
                        apt.add_conversation_message("assistant", history_reply)
                        # Sync state but DO NOT clear is_delayed for acknowledgements.
                        _sync_state_after_email(apt, plain_reply, dry_run, intent)
                        subject = _reply_subject(apt, intent)
                        _send_reply(apt, subject, html_body)
                        _set_ack_replied(apt)

                    _mark_seen(imap, uid)
                    processed += 1
                    continue

                # ── Plumbot reply (full Hormozi-framework path) ───────────────
                reply_text = _generate_plumbot_email_reply(clean, apt, context_note)
                out(f"    Reply preview: {reply_text[:80]}{'…' if len(reply_text) > 80 else ''}")

                if not dry_run:
                    # Save content only — strip sign-off so WhatsApp bot doesn't
                    # see "Takudzwa\nHomeBase Plumbers" as conversation content.
                    history_reply = _strip_signature_for_history(reply_text)
                    apt.add_conversation_message("assistant", history_reply)

                    # Keep WhatsApp state flags in sync with what was covered by email.
                    _sync_state_after_email(apt, reply_text, dry_run, intent)

                    subject   = _reply_subject(apt, intent)
                    html_body = _build_email_html(reply_text, apt)
                    _send_reply(apt, subject, html_body)

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
