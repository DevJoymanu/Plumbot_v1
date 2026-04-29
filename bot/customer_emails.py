"""
bot/customer_emails.py
======================
Customer-facing HTML email utilities.

All subjects include [APT-{id}] so inbound email replies can be matched
back to the correct appointment by the IMAP poller.
"""

import logging
import pytz

from django.conf import settings

logger = logging.getLogger(__name__)

_SAST            = pytz.timezone("Africa/Johannesburg")
_PLUMBER_PHONE   = "263774819901"  # fallback if no plumber number on appointment


# ── Formatting helpers ────────────────────────────────────────────────────────

def _to_sast(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return _SAST.localize(dt)
    return dt.astimezone(_SAST)


def _fmt_date(apt):
    dt = _to_sast(getattr(apt, "scheduled_datetime", None))
    if not dt:
        return "Scheduled date"
    days   = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    months = ["January","February","March","April","May","June",
              "July","August","September","October","November","December"]
    d = dt.date()
    return f"{days[d.weekday()]}, {d.day} {months[d.month - 1]} {d.year}"


def _fmt_time(apt):
    dt = _to_sast(getattr(apt, "scheduled_datetime", None))
    return dt.strftime("%H:%M") if dt else "Scheduled time"


def _clean_phone(raw):
    return "".join(c for c in (raw or "") if c.isdigit())


def _service(apt):
    svc = getattr(apt, "project_type", "") or ""
    return svc.replace("_", " ").title() or "Plumbing Service"


def _area(apt):
    return getattr(apt, "customer_area", "") or "your area"


def _plumber_phone(apt):
    raw = getattr(apt, "plumber_contact_number", "") or _PLUMBER_PHONE
    return _clean_phone(raw) or _PLUMBER_PHONE


def _apt_tag(apt):
    return f"[APT-{apt.pk}]"


# ── HTML builders ─────────────────────────────────────────────────────────────

def _apt_card(apt):
    """Appointment details card for customer emails."""
    plumber = _plumber_phone(apt)
    return (
        '<div style="border:1px solid #e0e0e0;border-radius:8px;padding:18px 22px;'
        'margin:16px 0;background:#f9f9f9;">'
        f'<p style="margin:0 0 10px;font-size:16px;font-weight:bold;color:#1a1a1a;">'
        f'📅 {_fmt_date(apt)} at {_fmt_time(apt)}</p>'
        f'<p style="margin:4px 0;color:#333;font-size:14px;">'
        f'🔧 <strong>Service:</strong> {_service(apt)}</p>'
        f'<p style="margin:4px 0;color:#333;font-size:14px;">'
        f'📍 <strong>Area:</strong> {_area(apt)}</p>'
        '<div style="margin-top:14px;">'
        f'<a href="tel:+{plumber}" style="display:inline-block;background:#25D366;'
        'color:#fff;text-decoration:none;padding:9px 16px;border-radius:5px;'
        'font-size:13px;margin-right:8px;margin-bottom:8px;">📞 Call Plumber</a>'
        f'<a href="https://wa.me/{plumber}" style="display:inline-block;'
        'background:#25D366;color:#fff;text-decoration:none;padding:9px 16px;'
        'border-radius:5px;font-size:13px;margin-bottom:8px;">💬 WhatsApp</a>'
        '</div>'
        '</div>'
    )


def _wrap(header_color, header_title, body_html):
    """Wrap body_html in a clean mobile-friendly HTML shell."""
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
        '<p style="margin:0;color:#aaa;font-size:12px;">HomeBase Plumbers · Zimbabwe</p>'
        '<p style="margin:4px 0;color:#aaa;font-size:12px;">'
        'To reschedule or cancel, simply reply to this email.</p>'
        '</div>'
        '</div></body></html>'
    )


def _send(apt, subject, html):
    """Send email to the customer with APT tag in subject."""
    from bot.plumber_notifications import send_email_to_recipients
    email = getattr(apt, "customer_email", None)
    if not email:
        logger.warning("No customer_email on appointment %s — skipping", apt.pk)
        return False
    tagged = f"{subject} {_apt_tag(apt)}"
    plain  = f"Please view this email in an HTML-compatible client.\n\nSubject: {tagged}"
    return send_email_to_recipients([email], tagged, plain, html_message=html)


# ── Public send functions ─────────────────────────────────────────────────────

def send_booking_confirmation_email(apt):
    """Send HTML booking confirmation to the customer immediately after booking."""
    try:
        name    = getattr(apt, "customer_name", "") or "there"
        subject = f"✅ Booking Confirmed — {_service(apt)} on {_fmt_date(apt)}"
        plumber = _plumber_phone(apt)
        body    = (
            f'<p>Hi {name},</p>'
            '<p>Your appointment is <strong>confirmed</strong>. '
            'Here are your booking details:</p>'
            f'{_apt_card(apt)}'
            '<p><strong>What to expect:</strong></p>'
            '<ul style="padding-left:20px;color:#333;">'
            '<li>Our senior plumber will call you 30 minutes before arrival</li>'
            '<li>Please ensure someone is home and the work area is accessible</li>'
            '<li>Water may need to be shut off temporarily during the visit</li>'
            '</ul>'
            '<p>Need to reschedule or cancel? Reply to this email with your '
            'preferred new date and time and we\'ll sort it out.</p>'
            '<p>See you then! 🔧</p>'
            '<p><strong>HomeBase Plumbers</strong></p>'
        )
        html = _wrap("#25D366", "✅ Appointment Confirmed", body)
        ok   = _send(apt, subject, html)
        if ok:
            logger.info("Booking confirmation email sent — apt %s", apt.pk)
        return ok
    except Exception:
        logger.exception("send_booking_confirmation_email failed — apt %s", apt.pk)
        return False


def send_delay_quote_email(apt, follow_up_date_str=None):
    """
    Send HTML quote + capabilities overview to a delayed lead.
    Called after the customer provides their email in the delay signal flow.
    """
    try:
        name         = getattr(apt, "customer_name", "") or "there"
        service_hint = f" for {_service(apt)}" if _service(apt) != "Plumbing Service" else ""
        plumber      = _plumber_phone(apt)
        followup_blk = (
            f'<p>We\'ll also follow up with you around '
            f'<strong>{follow_up_date_str}</strong> to see how things are going.</p>'
        ) if follow_up_date_str else ""

        subject = f"📋 Your Quote from HomeBase Plumbers{service_hint}"
        body    = (
            f'<p>Hi {name},</p>'
            '<p>As promised, here\'s an overview of our services and a general '
            f'pricing guide to help you plan ahead{service_hint}.</p>'
            '<hr style="border:none;border-top:1px solid #e0e0e0;margin:16px 0;">'
            '<p style="font-weight:bold;font-size:16px;">What We Offer</p>'
            '<ul style="padding-left:20px;color:#333;">'
            '<li>Bathroom Renovations (full supply &amp; fit)</li>'
            '<li>Kitchen Plumbing</li>'
            '<li>Geyser Installation &amp; Replacement</li>'
            '<li>Toilet &amp; Basin Supply and Fitting</li>'
            '<li>Shower Cubicles &amp; Vanity Units</li>'
            '<li>Pipe Repairs &amp; Drain Unblocking</li>'
            '<li>New Plumbing Installations</li>'
            '</ul>'
            '<hr style="border:none;border-top:1px solid #e0e0e0;margin:16px 0;">'
            '<p style="font-weight:bold;font-size:16px;">General Pricing Guide (USD)</p>'
            '<ul style="padding-left:20px;color:#333;">'
            '<li>Labour from <strong>$20</strong> for simple fittings</li>'
            '<li>Full bathroom renovation from <strong>$800</strong></li>'
            '<li>Geyser replacement from <strong>$350</strong></li>'
            '<li>Toilet supply &amp; fit from <strong>$120</strong></li>'
            '<li>Free on-site quote — no obligation</li>'
            '</ul>'
            '<hr style="border:none;border-top:1px solid #e0e0e0;margin:16px 0;">'
            f'{followup_blk}'
            '<p>When you\'re ready to move forward, simply reply to this email '
            'with your preferred date and we\'ll get you booked in. '
            'Or WhatsApp us directly:</p>'
            '<div style="margin-top:12px;">'
            f'<a href="https://wa.me/{plumber}" style="display:inline-block;'
            'background:#25D366;color:#fff;text-decoration:none;padding:10px 18px;'
            'border-radius:5px;font-size:14px;">💬 Message Us on WhatsApp</a>'
            '</div>'
            '<p style="margin-top:20px;">No pressure — we\'ll be right here '
            'whenever you\'re ready. 😊</p>'
            '<p><strong>HomeBase Plumbers</strong></p>'
        )
        html = _wrap("#1a73e8", "📋 Your Quote from HomeBase Plumbers", body)
        ok   = _send(apt, subject, html)
        if ok:
            logger.info("Delay quote email sent — apt %s", apt.pk)
        return ok
    except Exception:
        logger.exception("send_delay_quote_email failed — apt %s", apt.pk)
        return False


_REMINDER_CONFIGS = {
    'two_days': {
        'subject':      "📅 Appointment Reminder — {service} on {date}",
        'header_color': '#1a73e8',
        'header':       '📅 Your Appointment is in 2 Days',
        'intro':        'Just a heads-up — your appointment is coming up in <strong>2 days</strong>.',
        'footer':       'Please ensure someone is home and the work area is accessible.',
    },
    'one_day': {
        'subject':      "🗓️ Tomorrow's Appointment — {service} at {time}",
        'header_color': '#e65c00',
        'header':       '🗓️ Your Appointment is Tomorrow',
        'intro':        'Your appointment is <strong>tomorrow</strong>!',
        'footer':       'Please ensure someone is home and water can be shut off if needed.',
    },
    'morning': {
        'subject':      "☀️ Today's Appointment — {service} at {time}",
        'header_color': '#e65c00',
        'header':       "☀️ Your Appointment is Today",
        'intro':        'Good morning! Your plumber is arriving <strong>today at {time}</strong>.',
        'footer':       'Our plumber will call you 30 minutes before arrival.',
    },
    'two_hours': {
        'subject':      "⏰ Plumber Arriving in 2 Hours — {time}",
        'header_color': '#e53935',
        'header':       '⏰ Your Plumber Arrives in 2 Hours',
        'intro':        'Your plumber is on the way — arriving in approximately <strong>2 hours</strong> (at {time}).',
        'footer':       'Please ensure access is ready.',
    },
    'thirty_mins': {
        'subject':      "🚨 Plumber Arriving in 30 Minutes — {time}",
        'header_color': '#b71c1c',
        'header':       '🚨 Your Plumber is 30 Minutes Away',
        'intro':        'Your plumber is <strong>30 minutes away</strong> — heading to you now!',
        'footer':       'Please make sure the entrance is accessible.',
    },
}


def send_customer_reminder_email(apt, reminder_type):
    """
    Send HTML reminder email to the customer.

    reminder_type: 'two_days' | 'one_day' | 'morning' | 'two_hours' | 'thirty_mins'
    """
    try:
        cfg  = _REMINDER_CONFIGS.get(reminder_type, _REMINDER_CONFIGS['one_day'])
        name = getattr(apt, "customer_name", "") or "there"
        plumber = _plumber_phone(apt)
        t    = _fmt_time(apt)
        d    = _fmt_date(apt)
        svc  = _service(apt)

        subject = cfg['subject'].format(service=svc, date=d, time=t)
        intro   = cfg['intro'].format(time=t, date=d, name=name)
        body    = (
            f'<p>Hi {name},</p>'
            f'<p>{intro}</p>'
            f'{_apt_card(apt)}'
            f'<p>{cfg["footer"]}</p>'
            '<p>Need to reschedule or cancel? Reply to this email with your '
            'preferred new date and time.</p>'
            '<div style="margin-top:12px;">'
            f'<a href="https://wa.me/{plumber}" style="display:inline-block;'
            'background:#25D366;color:#fff;text-decoration:none;padding:9px 16px;'
            'border-radius:5px;font-size:13px;">💬 Message Us on WhatsApp</a>'
            '</div>'
        )
        html = _wrap(cfg['header_color'], cfg['header'], body)
        ok   = _send(apt, subject, html)
        if ok:
            logger.info("Customer reminder email (%s) sent — apt %s", reminder_type, apt.pk)
        return ok
    except Exception:
        logger.exception("send_customer_reminder_email failed — apt %s (%s)", apt.pk, reminder_type)
        return False


def send_email_reply_notification_to_plumber(apt, customer_reply_text):
    """
    Notify the plumber by email when a customer replies to an email.
    Called by the IMAP poller after processing an inbound customer email.
    """
    try:
        from bot.plumber_notifications import send_plumber_notification_email
        name    = getattr(apt, "customer_name", "") or "Unknown"
        phone   = getattr(apt, "phone_number", "") or ""
        subject = f"📧 Email Reply from {name} — {_service(apt)} {_apt_tag(apt)}"
        plumber_phone = _plumber_phone(apt)
        body    = (
            f'<p>Hi Team,</p>'
            f'<p>You have received an email reply from '
            f'<strong>{name}</strong> regarding their appointment.</p>'
            f'{_apt_card(apt)}'
            '<hr style="border:none;border-top:1px solid #e0e0e0;margin:16px 0;">'
            '<p style="font-weight:bold;">Customer\'s Message:</p>'
            f'<div style="background:#f9f9f9;border-left:4px solid #1a73e8;'
            f'padding:12px 16px;border-radius:4px;font-style:italic;color:#333;">'
            f'{customer_reply_text}</div>'
            '<hr style="border:none;border-top:1px solid #e0e0e0;margin:16px 0;">'
            '<div style="margin-top:12px;">'
            f'<a href="tel:+{plumber_phone}" style="display:inline-block;background:#25D366;'
            'color:#fff;text-decoration:none;padding:9px 16px;border-radius:5px;'
            'font-size:13px;margin-right:8px;">📞 Call Customer</a>'
            f'<a href="https://wa.me/{_clean_phone(phone)}" style="display:inline-block;'
            'background:#25D366;color:#fff;text-decoration:none;padding:9px 16px;'
            'border-radius:5px;font-size:13px;">💬 WhatsApp Customer</a>'
            '</div>'
        )
        html = _wrap("#1a73e8", f"📧 Email Reply from {name}", body)
        plain = f"Email reply from {name} (apt #{apt.pk}):\n\n{customer_reply_text}"
        return send_plumber_notification_email(subject, plain, html_message=html)
    except Exception:
        logger.exception("send_email_reply_notification_to_plumber failed — apt %s", apt.pk)
        return False
