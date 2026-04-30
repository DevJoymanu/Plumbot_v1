"""
bot/customer_emails.py
======================
Customer-facing HTML email utilities.

Every subject includes [APT-{id}] so inbound email replies can be matched
back to the correct appointment by the IMAP poller.

Design decisions:
- Minimal HTML so Gmail routes to Primary, not Promotions
- WhatsApp button always links to the business WhatsApp (+263776255077)
- Call button links to the plumber's direct line
- No "reply to reschedule" copy — all changes are nudged to WhatsApp
- Delay quote email attaches a PDF portfolio
"""

import logging
import os
from io import BytesIO

import pytz

from django.conf import settings

logger = logging.getLogger(__name__)

_SAST          = pytz.timezone("Africa/Johannesburg")
_PLUMBER_PHONE = "263774819901"     # fallback call number
_WA_NUMBER     = "263776255077"     # business WhatsApp (fixed)


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


def _call_phone(apt):
    """Direct call number — plumber's line."""
    raw = getattr(apt, "plumber_contact_number", "") or _PLUMBER_PHONE
    return _clean_phone(raw) or _PLUMBER_PHONE


def _apt_tag(apt):
    return f"[APT-{apt.pk}]"


# ── HTML builders (minimal — keeps Gmail routing to Primary) ──────────────────

def _apt_card(apt):
    """Compact appointment card with call + WhatsApp buttons."""
    call   = _call_phone(apt)
    return (
        '<div style="border-left:4px solid #25D366;padding:12px 16px;'
        'margin:16px 0;background:#f9f9f9;border-radius:0 6px 6px 0;">'
        f'<p style="margin:0 0 6px;font-size:15px;font-weight:bold;color:#111;">'
        f'📅 {_fmt_date(apt)} at {_fmt_time(apt)}</p>'
        f'<p style="margin:3px 0;color:#444;font-size:14px;">🔧 {_service(apt)}</p>'
        f'<p style="margin:3px 0;color:#444;font-size:14px;">📍 {_area(apt)}</p>'
        '<p style="margin:10px 0 0;">'
        f'<a href="tel:+{call}" style="background:#444;color:#fff;text-decoration:none;'
        f'padding:7px 14px;border-radius:4px;font-size:13px;margin-right:8px;">📞 Call</a>'
        f'<a href="https://wa.me/{_WA_NUMBER}" style="background:#25D366;color:#fff;'
        f'text-decoration:none;padding:7px 14px;border-radius:4px;font-size:13px;">'
        f'💬 WhatsApp</a>'
        '</p>'
        '</div>'
    )


def _wa_nudge():
    """WhatsApp nudge — used instead of "reply to this email" copy."""
    return (
        '<p style="margin:20px 0 0;font-size:14px;color:#555;">'
        f'For any changes, message us on WhatsApp — '
        f'<a href="https://wa.me/{_WA_NUMBER}" style="color:#25D366;font-weight:bold;">'
        f'tap here to chat</a>.'
        '</p>'
    )


def _wrap(body_html):
    """Minimal HTML wrapper — clean, not promotional."""
    return (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '</head>'
        '<body style="margin:0;padding:20px;background:#ffffff;'
        'font-family:Arial,sans-serif;font-size:15px;color:#333;line-height:1.6;">'
        f'{body_html}'
        '<p style="margin-top:32px;font-size:12px;color:#aaa;border-top:1px solid #eee;'
        'padding-top:12px;">HomeBase Plumbers · Zimbabwe</p>'
        '</body></html>'
    )


def _send(apt, subject, html, attachment=None, attachment_name="HomeBase_Portfolio.pdf"):
    """Send email to the customer with APT tag in subject."""
    from bot.plumber_notifications import send_email_to_recipients
    email = getattr(apt, "customer_email", None)
    if not email:
        logger.warning("No customer_email on appointment %s — skipping", apt.pk)
        return False
    tagged = f"{subject} {_apt_tag(apt)}"
    plain  = f"Please view this email in an HTML-compatible client.\n\n{subject}"
    return send_email_to_recipients(
        [email], tagged, plain,
        html_message=html,
        attachment=attachment,
        attachment_name=attachment_name,
    )


# ── PDF portfolio generator ───────────────────────────────────────────────────

_PRICING_ROWS = [
    ("Bathroom Renovation",           "Supply & fit all fixtures",       "From US$800"),
    ("Kitchen Renovation",            "Plumbing supply & fit",           "From US$600"),
    ("Geyser Installation",           "Supply & install",                "From US$350"),
    ("Toilet Supply & Fit",           "Supply, install & connect",       "From US$100"),
    ("Shower Cubicle",                "Supply from US$130 + install",    "From US$170"),
    ("Vanity Unit",                   "Supply from US$150 + install",    "From US$180"),
    ("Freestanding Tub",              "Supply from US$400 + install",    "From US$520"),
    ("Standard Bathtub",             "Supply from US$80 + install",     "From US$160"),
    ("Drain Unblocking",              "Labour only",                     "From US$20"),
    ("Pipe Repair / Leak",            "Labour only",                     "From US$15"),
    ("Geyser Repair",                 "Labour + parts",                  "From US$25"),
    ("Toilet Repair",                 "Labour + parts",                  "From US$20"),
]


def generate_portfolio_pdf():
    """
    Generate a PDF portfolio with previous project photos and full pricing.
    Returns bytes of the PDF.
    Photos are loaded from PREVIOUS_WORK_IMAGES_DIR (env var).
    Falls back gracefully if the folder is empty or missing.
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            Image as RLImage, HRFlowable,
        )

        buffer  = BytesIO()
        W, H    = A4
        margin  = 1.8 * cm

        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            leftMargin=margin, rightMargin=margin,
            topMargin=margin, bottomMargin=margin,
        )

        styles  = getSampleStyleSheet()
        green   = colors.HexColor("#25D366")
        dark    = colors.HexColor("#1a1a1a")
        mid     = colors.HexColor("#555555")
        light   = colors.HexColor("#f4f4f4")

        h1 = ParagraphStyle("h1", parent=styles["Heading1"],
                            fontSize=22, textColor=dark, spaceAfter=4)
        h2 = ParagraphStyle("h2", parent=styles["Heading2"],
                            fontSize=14, textColor=green, spaceAfter=6, spaceBefore=14)
        body = ParagraphStyle("body", parent=styles["Normal"],
                              fontSize=10, textColor=mid, leading=14)
        small = ParagraphStyle("small", parent=styles["Normal"],
                               fontSize=8, textColor=mid)

        elems = []

        # ── Header ────────────────────────────────────────────────────────────
        elems.append(Paragraph("HomeBase Plumbers", h1))
        elems.append(Paragraph("Zimbabwe's trusted plumbing specialists", body))
        elems.append(HRFlowable(width="100%", thickness=2, color=green,
                                spaceAfter=16, spaceBefore=8))

        # ── Previous Projects ─────────────────────────────────────────────────
        elems.append(Paragraph("Our Previous Work", h2))

        photos = _get_photo_paths()
        if photos:
            # Lay out photos in a 2-column grid
            available_w = W - 2 * margin
            cell_w      = (available_w - 0.5 * cm) / 2
            max_h       = 5.5 * cm
            photo_pairs = [photos[i:i + 2] for i in range(0, len(photos), 2)]
            for pair in photo_pairs:
                row = []
                for path in pair:
                    try:
                        img = RLImage(path, width=cell_w, height=max_h)
                        img.hAlign = "CENTER"
                        row.append(img)
                    except Exception:
                        row.append(Paragraph("(photo)", small))
                while len(row) < 2:
                    row.append("")
                t = Table([row], colWidths=[cell_w, cell_w])
                t.setStyle(TableStyle([
                    ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN",       (0, 0), (-1, -1), "CENTER"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING",(0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING",(0,0), (-1,-1), 6),
                ]))
                elems.append(t)
        else:
            elems.append(Paragraph(
                "Portfolio photos available on request — message us on WhatsApp "
                f"(+{_WA_NUMBER}) to see examples of our completed projects.",
                body,
            ))

        elems.append(Spacer(1, 0.4 * cm))
        elems.append(HRFlowable(width="100%", thickness=1, color=light,
                                spaceAfter=6, spaceBefore=6))

        # ── Pricing Guide ─────────────────────────────────────────────────────
        elems.append(Paragraph("Services & Pricing Guide", h2))
        elems.append(Paragraph(
            "All prices are in USD. Labour and supply prices vary by job scope, "
            "fixtures chosen, and site conditions. A free on-site assessment gives "
            "you an exact quote with no obligation.",
            body,
        ))
        elems.append(Spacer(1, 0.3 * cm))

        header_row = [
            Paragraph("<b>Service</b>", small),
            Paragraph("<b>Description</b>", small),
            Paragraph("<b>Starting From</b>", small),
        ]
        col_w = [6 * cm, 6.5 * cm, 4 * cm]
        table_data = [header_row] + [
            [Paragraph(s, small), Paragraph(d, small), Paragraph(p, small)]
            for s, d, p in _PRICING_ROWS
        ]
        price_table = Table(table_data, colWidths=col_w)
        price_table.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0),  green),
            ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f8f8")]),
            ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elems.append(price_table)

        elems.append(Spacer(1, 0.5 * cm))
        elems.append(HRFlowable(width="100%", thickness=1, color=light,
                                spaceAfter=8, spaceBefore=8))

        # ── Footer ────────────────────────────────────────────────────────────
        elems.append(Paragraph(
            f"Ready to book? WhatsApp us: +{_WA_NUMBER} | "
            f"Call: +{_PLUMBER_PHONE}",
            body,
        ))
        elems.append(Paragraph(
            "All work is carried out by experienced, licensed plumbers. "
            "We offer a satisfaction guarantee on all jobs.",
            small,
        ))

        doc.build(elems)
        return buffer.getvalue()

    except Exception:
        logger.exception("generate_portfolio_pdf failed")
        return None


def _get_photo_paths():
    """Return sorted list of photo file paths from the previous work folder."""
    try:
        from bot.whatsapp_webhook import (
            PREVIOUS_WORK_IMAGES_DIR,
            SUPPORTED_IMAGE_EXTENSIONS,
        )
        if not os.path.exists(PREVIOUS_WORK_IMAGES_DIR):
            return []
        return sorted(
            os.path.join(PREVIOUS_WORK_IMAGES_DIR, f)
            for f in os.listdir(PREVIOUS_WORK_IMAGES_DIR)
            if os.path.splitext(f)[1].lower() in SUPPORTED_IMAGE_EXTENSIONS
        )
    except Exception:
        return []


# ── Public send functions ─────────────────────────────────────────────────────

def send_booking_confirmation_email(apt):
    """Send HTML booking confirmation to the customer immediately after booking."""
    try:
        name    = getattr(apt, "customer_name", "") or "there"
        subject = f"Booking Confirmed — {_service(apt)} on {_fmt_date(apt)}"
        body    = (
            f'<p>Hi {name},</p>'
            f'<p>Your appointment is confirmed. Here are the details:</p>'
            f'{_apt_card(apt)}'
            '<p>Our plumber will call you 30 minutes before arrival. '
            'Please ensure someone is home and the work area is accessible.</p>'
            f'{_wa_nudge()}'
            '<p>See you then! 🔧<br><strong>HomeBase Plumbers</strong></p>'
        )
        html = _wrap(body)
        ok   = _send(apt, subject, html)
        if ok:
            logger.info("Booking confirmation email sent — apt %s", apt.pk)
        return ok
    except Exception:
        logger.exception("send_booking_confirmation_email failed — apt %s", apt.pk)
        return False


def send_delay_quote_email(apt, follow_up_date_str=None):
    """
    Send HTML quote + portfolio email to a delayed lead.
    Attaches a PDF portfolio with project photos and pricing.
    """
    try:
        name         = getattr(apt, "customer_name", "") or "there"
        service_hint = f" for {_service(apt)}" if _service(apt) != "Plumbing Service" else ""
        followup_blk = (
            f'<p>I\'ll also follow up with you around '
            f'<strong>{follow_up_date_str}</strong>.</p>'
        ) if follow_up_date_str else ""

        subject = f"Your Quote from HomeBase Plumbers{service_hint}"
        body    = (
            f'<p>Hi {name},</p>'
            '<p>As promised — attached is our portfolio with examples of previous '
            f'projects and our full pricing guide{service_hint}.</p>'
            f'{followup_blk}'
            '<p>When you\'re ready to move forward, the quickest way to reach us is '
            f'on WhatsApp:</p>'
            f'<p><a href="https://wa.me/{_WA_NUMBER}" style="background:#25D366;'
            'color:#fff;text-decoration:none;padding:9px 18px;border-radius:5px;'
            'font-size:14px;display:inline-block;">💬 Message Us on WhatsApp</a></p>'
            '<p>No pressure — we\'ll be right here whenever you\'re ready. 😊</p>'
            '<p><strong>HomeBase Plumbers</strong></p>'
        )
        html = _wrap(body)

        # Generate and attach PDF portfolio
        pdf  = generate_portfolio_pdf()
        ok   = _send(
            apt, subject, html,
            attachment=pdf,
            attachment_name="HomeBase_Plumbers_Portfolio.pdf",
        )
        if ok:
            logger.info("Delay quote email sent — apt %s", apt.pk)
        return ok
    except Exception:
        logger.exception("send_delay_quote_email failed — apt %s", apt.pk)
        return False


_REMINDER_CONFIGS = {
    'two_days': {
        'subject': "Appointment Reminder — {service} on {date}",
        'intro':   'Your appointment is coming up in <strong>2 days</strong>.',
        'footer':  'Please ensure someone is home and the work area is accessible.',
    },
    'one_day': {
        'subject': "Your Appointment is Tomorrow — {service} at {time}",
        'intro':   'Your appointment is <strong>tomorrow</strong>.',
        'footer':  'Please ensure someone is home and water can be shut off if needed.',
    },
    'morning': {
        'subject': "Your Appointment is Today — {service} at {time}",
        'intro':   'Good morning! Your plumber arrives <strong>today at {time}</strong>.',
        'footer':  'Our plumber will call you 30 minutes before arrival.',
    },
    'two_hours': {
        'subject': "Your Plumber Arrives in 2 Hours — {time}",
        'intro':   'Your plumber is on the way — arriving in approximately <strong>2 hours</strong>.',
        'footer':  'Please ensure access is ready.',
    },
    'thirty_mins': {
        'subject': "Your Plumber is 30 Minutes Away — {time}",
        'intro':   'Your plumber is <strong>30 minutes away</strong>.',
        'footer':  'Please make sure the entrance is accessible.',
    },
}


def send_customer_reminder_email(apt, reminder_type):
    """Send HTML reminder email to the customer."""
    try:
        cfg  = _REMINDER_CONFIGS.get(reminder_type, _REMINDER_CONFIGS['one_day'])
        name = getattr(apt, "customer_name", "") or "there"
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
            f'{_wa_nudge()}'
        )
        html = _wrap(body)
        ok   = _send(apt, subject, html)
        if ok:
            logger.info("Customer reminder email (%s) sent — apt %s", reminder_type, apt.pk)
        return ok
    except Exception:
        logger.exception("send_customer_reminder_email failed — apt %s (%s)", apt.pk, reminder_type)
        return False


def send_email_reply_notification_to_plumber(apt, customer_reply_text):
    """Notify the plumber by email when a customer replies to an email."""
    try:
        from bot.plumber_notifications import send_plumber_notification_email
        name    = getattr(apt, "customer_name", "") or "Unknown"
        call    = _call_phone(apt)
        phone   = getattr(apt, "phone_number", "") or ""
        subject = f"Email Reply from {name} — {_service(apt)} {_apt_tag(apt)}"
        body    = (
            f'<p>Hi Team,</p>'
            f'<p>Email reply received from <strong>{name}</strong>.</p>'
            f'{_apt_card(apt)}'
            '<p><strong>Customer\'s message:</strong></p>'
            f'<blockquote style="border-left:3px solid #25D366;margin:0;padding:8px 16px;'
            f'color:#555;font-style:italic;">{customer_reply_text}</blockquote>'
        )
        html  = _wrap(body)
        plain = f"Email reply from {name} (apt #{apt.pk}):\n\n{customer_reply_text}"
        return send_plumber_notification_email(subject, plain, html_message=html)
    except Exception:
        logger.exception("send_email_reply_notification_to_plumber failed — apt %s", apt.pk)
        return False
