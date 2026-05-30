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
import threading
import time
from io import BytesIO

import pytz

from django.conf import settings
from django.db import close_old_connections

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


def _contact_buttons(call):
    """WhatsApp + Call buttons — outlined (no fill) to avoid Gmail Promotions routing."""
    return (
        '<p style="margin:16px 0;line-height:1;">'
        f'<a href="https://wa.me/{_WA_NUMBER}" style="display:inline-block;'
        f'border:1.5px solid #1a9e4a;color:#1a9e4a;text-decoration:none;'
        f'padding:9px 16px;border-radius:4px;font-size:14px;font-weight:bold;'
        f'margin-right:10px;">💬 WhatsApp</a>'
        f'<a href="tel:+{call}" style="display:inline-block;'
        f'border:1.5px solid #555;color:#333;text-decoration:none;'
        f'padding:9px 16px;border-radius:4px;font-size:14px;">📞 Call Takudzwa</a>'
        '</p>'
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


def _customer_contact_buttons(customer_phone_digits):
    """
    Call + WhatsApp buttons that target the CUSTOMER — for plumber-facing
    emails (e.g. new-booking notifications), where the plumber needs to reach
    the customer, not the business line.
    """
    digits = _clean_phone(customer_phone_digits)
    if not digits:
        return ''
    return (
        '<p style="margin:16px 0;line-height:1;">'
        f'<a href="https://wa.me/{digits}" style="display:inline-block;'
        'border:1.5px solid #1a9e4a;color:#1a9e4a;text-decoration:none;'
        'padding:9px 16px;border-radius:4px;font-size:14px;font-weight:bold;'
        'margin-right:10px;">💬 WhatsApp customer</a>'
        f'<a href="tel:+{digits}" style="display:inline-block;'
        'border:1.5px solid #555;color:#333;text-decoration:none;'
        'padding:9px 16px;border-radius:4px;font-size:14px;">📞 Call customer</a>'
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
    """
    Send email to the customer.
    APT ID is encoded in the Message-ID header (invisible to the customer) rather
    than appended to the subject line.  The IMAP poller reads In-Reply-To to match
    replies back to the appointment, with a subject-tag fallback for legacy emails.
    """
    from bot.plumber_notifications import send_email_to_recipients
    email = getattr(apt, "customer_email", None)
    if not email:
        logger.warning("No customer_email on appointment %s — skipping", apt.pk)
        return False
    # Unique per-email Message-ID that encodes the appointment PK
    domain     = getattr(settings, "EMAIL_DOMAIN", "homebaseplumbers.co.zw")
    message_id = f"<apt-{apt.pk}.{int(time.time())}@{domain}>"
    plain = (
        f"{subject}\n\n"
        f"Service: {_service(apt)}\n"
        f"Area:    {_area(apt)}\n"
        f"Date:    {_fmt_date(apt)} at {_fmt_time(apt)}\n\n"
        f"WhatsApp: https://wa.me/{_WA_NUMBER}\n"
        f"Call Takudzwa: +{_call_phone(apt)}\n"
        f"HomeBase Plumbers"
    )
    return send_email_to_recipients(
        [email], subject, plain,
        html_message=html,
        attachment=attachment,
        attachment_name=attachment_name,
        from_name="Takudzwa",
        message_id=message_id,
    )


# ── PDF portfolio generator ───────────────────────────────────────────────────

# Photo captions — rotate through these based on photo index.
# Written to add emotion and context without knowing the exact photo content.
_PHOTO_CAPTIONS = [
    "A complete bathroom transformation — from bare walls to a space our client is proud to call theirs.",
    "Every family deserves a bathroom that works. We built this one from scratch in three days.",
    "The moment a tired, outdated bathroom becomes a clean, functional sanctuary.",
    "Precision fitting on a full freestanding tub setup — the kind of detail that makes the difference.",
    "New plumbing, new possibilities. This kitchen renovation gave a young family a fresh start.",
    "Two days of work. Years of enjoyment. This is what we do.",
    "Geyser installed and tested before the sun set — hot water restored, family relieved.",
    "A shower cubicle sealed right, fitted right, built to last. No shortcuts, no leaks.",
    "This bathroom was dark and damp. Now it's the first thing guests notice — for all the right reasons.",
    "Our senior plumber takes personal responsibility for every job, from planning to handover.",
    "A freestanding tub that turned a simple bathroom into a retreat. Supply, fit, and finished.",
    "Side chamber installed flush and level — small details that protect the whole renovation.",
]


def generate_portfolio_pdf():
    """
    Generate a PDF portfolio with previous project photos and full pricing.
    Returns bytes of the PDF, or None on failure.
    Photos come from PREVIOUS_WORK_IMAGES_DIR; falls back gracefully if empty.
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            Image as RLImage, HRFlowable, KeepTogether,
        )

        buffer = BytesIO()
        W, _H  = A4
        margin = 1.8 * cm

        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            leftMargin=margin, rightMargin=margin,
            topMargin=margin, bottomMargin=margin,
        )

        styles = getSampleStyleSheet()
        green  = colors.HexColor("#25D366")
        dark   = colors.HexColor("#1a1a1a")
        mid    = colors.HexColor("#555555")
        light  = colors.HexColor("#eeeeee")
        white  = colors.white

        h1 = ParagraphStyle("h1", parent=styles["Heading1"],
                            fontSize=24, textColor=dark, spaceAfter=2, spaceBefore=0)
        sub = ParagraphStyle("sub", parent=styles["Normal"],
                             fontSize=11, textColor=mid, spaceAfter=0)
        h2 = ParagraphStyle("h2", parent=styles["Heading2"],
                            fontSize=13, textColor=green, spaceAfter=4, spaceBefore=14,
                            fontName="Helvetica-Bold")
        body = ParagraphStyle("body", parent=styles["Normal"],
                              fontSize=9, textColor=mid, leading=13)
        caption = ParagraphStyle("cap", parent=styles["Normal"],
                                 fontSize=8, textColor=mid, leading=12,
                                 fontName="Helvetica-Oblique", spaceAfter=8)
        th = ParagraphStyle("th", parent=styles["Normal"],
                            fontSize=8, textColor=white, fontName="Helvetica-Bold")
        td = ParagraphStyle("td", parent=styles["Normal"],
                            fontSize=8, textColor=dark, leading=11)
        td_mid = ParagraphStyle("tdm", parent=styles["Normal"],
                                fontSize=8, textColor=mid, leading=11)
        note = ParagraphStyle("note", parent=styles["Normal"],
                              fontSize=7, textColor=mid, leading=10,
                              fontName="Helvetica-Oblique")

        def tbl(data, col_widths, header=True):
            """Build a styled table. Row 0 is the header if header=True."""
            t = Table(data, colWidths=col_widths, repeatRows=1 if header else 0)
            ts = [
                ("TOPPADDING",    (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING",   (0, 0), (-1, -1), 5),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
                ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#dddddd")),
                ("ROWBACKGROUNDS",(0, 1), (-1, -1),
                 [white, colors.HexColor("#f7f7f7")]),
            ]
            if header:
                ts += [
                    ("BACKGROUND", (0, 0), (-1, 0), green),
                    ("TEXTCOLOR",  (0, 0), (-1, 0), white),
                ]
            t.setStyle(TableStyle(ts))
            return t

        elems = []
        AW = W - 2 * margin   # available width

        # ── Header ────────────────────────────────────────────────────────────
        elems.append(Paragraph("HomeBase Plumbers", h1))
        elems.append(Paragraph("Zimbabwe's trusted plumbing specialists", sub))
        elems.append(HRFlowable(width="100%", thickness=2, color=green,
                                spaceAfter=14, spaceBefore=6))

        # ── Previous Projects ─────────────────────────────────────────────────
        elems.append(Paragraph("Our Previous Work", h2))
        elems.append(Paragraph(
            "Every project below was completed by our senior plumber with a focus on "
            "quality, cleanliness, and care for the client's home.",
            body,
        ))
        elems.append(Spacer(1, 0.3 * cm))

        photos = _get_photo_paths()
        if photos:
            cell_w = (AW - 0.4 * cm) / 2
            img_h  = 5.5 * cm
            for i in range(0, len(photos), 2):
                pair  = photos[i:i + 2]
                imgs  = []
                caps  = []
                for j, path in enumerate(pair):
                    idx = i + j
                    try:
                        img = RLImage(path, width=cell_w, height=img_h)
                        imgs.append(img)
                    except Exception:
                        imgs.append(Paragraph("(photo)", td_mid))
                    cap_text = _PHOTO_CAPTIONS[idx % len(_PHOTO_CAPTIONS)]
                    caps.append(Paragraph(cap_text, caption))
                while len(imgs) < 2:
                    imgs.append("")
                    caps.append("")
                img_row = Table([imgs], colWidths=[cell_w, cell_w])
                img_row.setStyle(TableStyle([
                    ("VALIGN",       (0, 0), (-1, -1), "TOP"),
                    ("ALIGN",        (0, 0), (-1, -1), "CENTER"),
                    ("LEFTPADDING",  (0, 0), (-1, -1), 2),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
                ]))
                cap_row = Table([caps], colWidths=[cell_w, cell_w])
                cap_row.setStyle(TableStyle([
                    ("VALIGN",       (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING",  (0, 0), (-1, -1), 2),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                    ("TOPPADDING",   (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING",(0, 0), (-1, -1), 8),
                ]))
                elems.append(KeepTogether([img_row, cap_row]))
        else:
            elems.append(Paragraph(
                "Portfolio photos available on request. Message us on WhatsApp "
                f"(+{_WA_NUMBER}) and we'll send examples of our completed work.",
                body,
            ))

        elems.append(Spacer(1, 0.2 * cm))
        elems.append(HRFlowable(width="100%", thickness=1, color=light,
                                spaceAfter=4, spaceBefore=4))

        # ── Pricing Guide ─────────────────────────────────────────────────────
        elems.append(Paragraph("Complete Services & Pricing Guide", h2))
        elems.append(Paragraph(
            "All prices are in USD. Supply and labour costs vary by fixture choice, "
            "site conditions, and scope of work. A free on-site assessment gives you "
            "an exact written quote with no obligation.",
            body,
        ))
        elems.append(Spacer(1, 0.25 * cm))

        # ── Section 1: Full Renovations ───────────────────────────────────────
        elems.append(Paragraph("Full Renovations", h2))
        c1, c2, c3, c4 = AW*0.35, AW*0.30, AW*0.18, AW*0.17
        reno_data = [
            [Paragraph(x, th) for x in ["Service", "What's Included", "Supply", "Labour"]],
            [Paragraph("Bathroom Renovation", td),
             Paragraph("All fixtures: tub/shower, vanity, toilet, chamber, all pipework", td_mid),
             Paragraph("Included", td_mid), Paragraph("From US$600", td)],
            [Paragraph("Kitchen Renovation", td),
             Paragraph("All kitchen plumbing: sink, pipes, drainage, connections", td_mid),
             Paragraph("Included", td_mid), Paragraph("From US$600", td)],
            [Paragraph("Full Bathroom Package", td),
             Paragraph("Shower cubicle + vanity + toilet + chamber + tub (fixtures of your choice)", td_mid),
             Paragraph("Included", td_mid), Paragraph("From US$600", td)],
        ]
        elems.append(tbl(reno_data, [c1, c2, c3, c4]))
        elems.append(Spacer(1, 0.2 * cm))

        # ── Section 2: Individual Bathroom Fittings ───────────────────────────
        elems.append(Paragraph("Individual Bathroom Fittings", h2))
        c1, c2, c3, c4 = AW*0.30, AW*0.25, AW*0.22, AW*0.23
        fit_data = [
            [Paragraph(x, th) for x in ["Item", "Supply (from)", "Install (from)", "All-In (from)"]],
            [Paragraph("Shower Cubicle", td),
             Paragraph("US$130", td_mid), Paragraph("US$40", td_mid), Paragraph("US$170", td)],
            [Paragraph("Vanity Unit", td),
             Paragraph("US$150", td_mid), Paragraph("US$30", td_mid), Paragraph("US$180", td)],
            [Paragraph("Toilet Seat & Cistern", td),
             Paragraph("US$50", td_mid), Paragraph("US$20", td_mid), Paragraph("US$70", td)],
            [Paragraph("Side Chamber", td),
             Paragraph("US$130", td_mid), Paragraph("US$30", td_mid), Paragraph("US$160", td)],
            [Paragraph("Standard Bathtub (1500×700)", td),
             Paragraph("US$80", td_mid), Paragraph("US$80", td_mid), Paragraph("US$160", td)],
            [Paragraph("Freestanding Tub", td),
             Paragraph("US$400", td_mid),
             Paragraph("Mixer US$150 + Install US$120", td_mid),
             Paragraph("US$670+", td)],
        ]
        elems.append(tbl(fit_data, [c1, c2, c3, c4]))
        elems.append(Spacer(1, 0.2 * cm))

        # ── Section 3: Geyser Services ────────────────────────────────────────
        elems.append(Paragraph("Geyser Services", h2))
        c1, c2, c3 = AW*0.38, AW*0.38, AW*0.24
        gey_data = [
            [Paragraph(x, th) for x in ["Service", "Detail", "Cost (from)"]],
            [Paragraph("Geyser Supply & Installation", td),
             Paragraph("New geyser supplied and fitted", td_mid),
             Paragraph("US$160 all-in\n(US$80 supply + US$80 labour)", td)],
            [Paragraph("Full Geyser Replacement", td),
             Paragraph("Remove old unit, supply & install new geyser", td_mid),
             Paragraph("US$350 all-in", td)],
            [Paragraph("Pressure Valve Replacement", td),
             Paragraph("Faulty valve replaced, system tested", td_mid),
             Paragraph("US$25 labour + parts", td)],
            [Paragraph("Thermostat Replacement", td),
             Paragraph("Replace failed thermostat, restore hot water", td_mid),
             Paragraph("US$30 labour + parts", td)],
            [Paragraph("Element Replacement", td),
             Paragraph("Replace heating element, full test", td_mid),
             Paragraph("US$40 labour + parts", td)],
        ]
        elems.append(tbl(gey_data, [c1, c2, c3]))
        elems.append(Spacer(1, 0.2 * cm))

        # ── Section 4: Repairs & Maintenance ─────────────────────────────────
        elems.append(Paragraph("Repairs & Maintenance", h2))
        c1, c2, c3 = AW*0.38, AW*0.38, AW*0.24
        rep_data = [
            [Paragraph(x, th) for x in ["Service", "Detail", "Cost (from)"]],
            [Paragraph("Leaking Tap", td),
             Paragraph("Washer or cartridge replacement", td_mid),
             Paragraph("US$15 labour", td)],
            [Paragraph("Toilet Seat Replacement", td),
             Paragraph("Supply new seat, fit and test", td_mid),
             Paragraph("US$20 supply + US$10 fit", td)],
            [Paragraph("Cistern Repair", td),
             Paragraph("Filling valve or flush valve replacement", td_mid),
             Paragraph("US$20 labour + parts", td)],
            [Paragraph("Leaking Toilet Base", td),
             Paragraph("Reseal base, test for leaks", td_mid),
             Paragraph("US$25 labour", td)],
            [Paragraph("Full Toilet Replacement", td),
             Paragraph("Remove old toilet, supply & install new unit", td_mid),
             Paragraph("US$60 supply + US$40 install", td)],
            [Paragraph("Drain Unblocking (simple)", td),
             Paragraph("Sink, basin, or shower drain cleared", td_mid),
             Paragraph("US$20 labour", td)],
            [Paragraph("Drain Unblocking (severe)", td),
             Paragraph("Main drain or sewer line blockage", td_mid),
             Paragraph("US$50 labour", td)],
            [Paragraph("High-Pressure Jetting", td),
             Paragraph("Stubborn or recurring blockages", td_mid),
             Paragraph("US$80", td)],
            [Paragraph("Minor Pipe Leak Repair", td),
             Paragraph("Joint or fitting leak, sealed and tested", td_mid),
             Paragraph("US$20 labour", td)],
            [Paragraph("Burst Pipe Repair", td),
             Paragraph("Emergency burst pipe, section repaired", td_mid),
             Paragraph("US$40 labour", td)],
            [Paragraph("Pipe Section Replacement", td),
             Paragraph("Corroded or damaged section replaced", td_mid),
             Paragraph("US$50 labour", td)],
        ]
        elems.append(tbl(rep_data, [c1, c2, c3]))

        elems.append(Spacer(1, 0.3 * cm))
        elems.append(Paragraph(
            "* Labour prices are for work only. Parts and fixtures are charged separately "
            "unless stated as all-in. All prices are starting rates — complex jobs may vary. "
            "We always confirm the final price before starting work.",
            note,
        ))
        elems.append(Spacer(1, 0.3 * cm))
        elems.append(HRFlowable(width="100%", thickness=1, color=light,
                                spaceAfter=8, spaceBefore=4))

        # ── Footer ────────────────────────────────────────────────────────────
        elems.append(Paragraph(
            f"Ready to book a free on-site assessment?  "
            f"WhatsApp: +{_WA_NUMBER}   |   Call: +{_PLUMBER_PHONE}",
            body,
        ))
        elems.append(Paragraph(
            "All work carried out by experienced, licensed plumbers. "
            "Satisfaction guaranteed on every job.",
            note,
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
        subject = f"Confirmed — {_service(apt)} on {_fmt_date(apt)}"
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
    Send quote + portfolio email to a delayed lead.
    Attaches a PDF portfolio with project photos and pricing.

    Deliverability: personal subject, two outlined CTA buttons (Call +
    WhatsApp), SendGrid link/open tracking disabled so links stay clean.
    """
    try:
        name         = getattr(apt, "customer_name", "") or ""
        hi           = f"Hi {name}" if name else "Hi there"
        service_hint = f" for {_service(apt)}" if _service(apt) != "Plumbing Service" else ""
        call         = _call_phone(apt)
        followup_blk = (
            f'<p style="margin:0 0 14px;">I\'ll also check in with you around '
            f'<strong>{follow_up_date_str}</strong> — no rush before then.</p>'
        ) if follow_up_date_str else ""

        subject = "Portfolio and pricing" + (f" — as requested, {name}" if name else " — as requested")

        # Prose + two outlined CTA buttons. Avoid bullets and the word "free"
        # (kept as "no cost"), which are promotional triggers; the buttons are
        # safe now that SendGrid link/open tracking is disabled, so links aren't
        # rewrapped. Same sales mechanics, in plain sentences = personal note.
        followup_inline = (
            f' I\'ll also check in with you around {follow_up_date_str} — no rush before then.'
            if follow_up_date_str else ''
        )
        body = (
            f'<p>{hi},</p>'
            f'<p>As promised — attached is our portfolio with previous projects '
            f'and the full pricing guide{service_hint}.{followup_inline}</p>'
            f'<p>Worth knowing before you decide: the on-site visit and written quote '
            f'cost nothing, you see the fixed price before any work starts, and we '
            f'don\'t leave until you\'re happy with the job.</p>'
            f'<p>If you\'d like to lock in a time, reply with a day that suits — '
            f'morning or afternoon — and I\'ll sort it. Or reach me directly:</p>'
            f'{_contact_buttons(call)}'
            f'<p>Takudzwa<br>HomeBase Plumbers<br>+{call}</p>'
        )

        html = (
            '<!DOCTYPE html><html><body>'
            f'{body}'
            '</body></html>'
        )

        pdf = generate_portfolio_pdf()
        if pdf is None:
            # Retry once — PDF generation can fail transiently on first run
            logger.warning("generate_portfolio_pdf returned None for apt %s — retrying", apt.pk)
            pdf = generate_portfolio_pdf()

        if pdf is None:
            logger.error(
                "Portfolio PDF could not be generated for apt %s — email NOT sent", apt.pk
            )
            return False

        ok = _send(
            apt, subject, html,
            attachment=pdf,
            attachment_name="HomeBase_Plumbers_Portfolio.pdf",
        )
        if ok:
            logger.info("Delay quote email sent with PDF — apt %s", apt.pk)
        else:
            logger.error("Delay quote email FAILED to send — apt %s", apt.pk)
        return ok
    except Exception:
        logger.exception("send_delay_quote_email failed — apt %s", apt.pk)
        return False


def send_delay_quote_email_async(apt, follow_up_date_str=None):
    """
    Queue the delay quote email without blocking the WhatsApp response path.

    Railway can silently drop outbound SMTP, which makes smtplib wait until its
    socket timeout. Running this in a daemon thread keeps the customer-facing
    WhatsApp flow responsive while preserving the same email logging.
    """
    apt_id = getattr(apt, "pk", None)
    if not apt_id:
        logger.warning("Delay quote email async skipped - appointment has no pk")
        return None

    def _worker():
        close_old_connections()
        try:
            from bot.models import Appointment
            fresh_apt = Appointment.objects.filter(pk=apt_id).first()
            if not fresh_apt:
                logger.warning("Delay quote email async skipped - apt %s not found", apt_id)
                return
            send_delay_quote_email(fresh_apt, follow_up_date_str=follow_up_date_str)
        except Exception:
            logger.exception("Delay quote email async worker failed - apt %s", apt_id)
        finally:
            close_old_connections()

    thread = threading.Thread(
        target=_worker,
        name=f"delay-quote-email-{apt_id}",
        daemon=True,
    )
    thread.start()
    return thread


_REMINDER_CONFIGS = {
    'two_days': {
        'subject': "See you on {date} — quick confirm?",
        'intro':   'Your appointment is coming up in <strong>2 days</strong>.',
        'footer':  ('Quick favour — reply <strong>YES</strong> on WhatsApp if all is still '
                    'good for {date}, or let us know if anything needs to shift. '
                    'Please ensure someone is home and the work area is accessible.'),
    },
    'one_day': {
        'subject': "Tomorrow at {time} — still good?",
        'intro':   'Your appointment is <strong>tomorrow</strong>.',
        'footer':  ('A quick <strong>YES</strong> on WhatsApp confirms you\'re still on for '
                    'tomorrow — that way we don\'t double-book the slot. '
                    'Please ensure someone is home and water can be shut off if needed.'),
    },
    'morning': {
        'subject': "Today at {time} — {service}",
        'intro':   'Good morning! Your plumber arrives <strong>today at {time}</strong>.',
        'footer':  'Our plumber will call you 30 minutes before arrival.',
    },
    'two_hours': {
        'subject': "On the way — arriving at {time}",
        'intro':   'Your plumber is on the way — arriving in approximately <strong>2 hours</strong>.',
        'footer':  'Please ensure access is ready.',
    },
    'thirty_mins': {
        'subject': "Arriving in 30 minutes — {time}",
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
        footer  = cfg['footer'].format(time=t, date=d, name=name)
        body    = (
            f'<p>Hi {name},</p>'
            f'<p>{intro}</p>'
            f'{_apt_card(apt)}'
            f'<p>{footer}</p>'
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


def _extract_conversation_context(apt):
    """
    Scan the WhatsApp conversation history for specific items the customer mentioned.
    Returns a short list of labels (e.g. ['freestanding tub', 'side chamber']).
    """
    history = getattr(apt, 'conversation_history', None) or []
    customer_text = ' '.join(
        m.get('content', '').lower()
        for m in history
        if m.get('role') == 'user'
    )
    item_map = [
        ('freestanding tub',  'freestanding tub'),
        ('standard tub',      'standard tub'),
        ('shower cubicle',    'shower cubicle'),
        ('vanity',            'vanity'),
        ('geyser',            'geyser'),
        ('toilet',            'toilet'),
        ('side chamber',      'side chamber'),
        ('chamber',           'side chamber'),
        ('kitchen',           'kitchen renovation'),
        ('tub',               'bathtub'),
        ('shower',            'shower'),
    ]
    seen = set()
    items = []
    for keyword, label in item_map:
        if keyword in customer_text and label not in seen:
            seen.add(label)
            items.append(label)
        if len(items) == 3:
            break
    return items


def send_delay_followup_email(apt):
    """
    Contextual re-engagement email sent on the agreed follow-up date.

    Deliverability design:
    - Subject is personal, no company name, no promotional language
    - Body reads as a one-to-one message from a real person (Takudzwa)
    - Two outlined CTA buttons (Call + WhatsApp); safe for Primary routing
      because SendGrid link/open tracking is disabled (clean tel:/wa.me links)
    - Minimal HTML structure, no logo header
    - Signed off with a real name and direct number
    All of these push Gmail to route to Primary, not Promotions.
    """
    try:
        name    = getattr(apt, 'customer_name', '') or ''
        hi      = f'Hi {name}' if name else 'Hi there'
        service = _service(apt)
        area    = _area(apt)
        desc    = (getattr(apt, 'project_description', '') or '').strip()
        call    = _call_phone(apt)

        # Build the specific project reference from what we know
        if desc:
            project_ref = desc[:100]
        elif service != 'Plumbing Service' and area != 'your area':
            project_ref = f'{service} in {area}'
        elif service != 'Plumbing Service':
            project_ref = service
        else:
            project_ref = f'your plumbing project in {area}' if area != 'your area' else 'your plumbing project'

        # Pull specific items from the WhatsApp conversation
        items = _extract_conversation_context(apt)
        items_detail = ''
        if items:
            items_detail = f' You specifically asked about {" and ".join(items[:2])}.'

        subject = 'Still on for your plumbing work' + (f', {name}?' if name else '?')

        # Prose + two outlined CTA buttons (Call + WhatsApp). With SendGrid
        # click/open tracking disabled (see _send_via_sendgrid), the tel:/wa.me
        # links stay clean, so the buttons no longer trigger Promotions routing.
        # The sales mechanics (risk reversal + micro-yes close) stay in natural
        # sentences so Gmail still reads this as a personal email.
        body = (
            f'<p>{hi},</p>'
            f'<p>Circling back as we agreed — you said you\'d be back around now '
            f'and were looking at {project_ref}.{items_detail}</p>'
            f'<p>Just so you know how it works: we come out and have a look at no cost, '
            f'agree the price in writing before any work starts, and we don\'t leave '
            f'until you\'re happy with the job.</p>'
            f'<p>If you want to take the next step, reply with a day that suits — '
            f'morning or afternoon — and I\'ll pop you in the diary. '
            f'Or reach me directly:</p>'
            f'{_contact_buttons(call)}'
            f'<p>Takudzwa<br>HomeBase Plumbers<br>+{call}</p>'
        )

        html = (
            '<!DOCTYPE html><html><body>'
            f'{body}'
            '</body></html>'
        )

        ok = _send(apt, subject, html)
        if ok:
            logger.info("Delay follow-up email sent — apt %s", apt.pk)
        return ok
    except Exception:
        logger.exception("send_delay_followup_email failed — apt %s", apt.pk)
        return False


def send_delay_last_check_email(apt):
    """
    Second (final) re-engagement email — sent a few days after
    send_delay_followup_email when the lead still hasn't responded.

    Copy intentionally short, different from the first touch, and explicitly
    leaves the door open (reply "later" to be closed out quietly). This is
    the last email we send to a delayed lead.

    Deliverability rules (same as the first touch):
    - Personal subject, no company name, no promotional language
    - Two outlined CTA buttons (Call + WhatsApp); SendGrid tracking disabled
    - Minimal HTML, signed off by Takudzwa with direct number
    """
    try:
        name    = getattr(apt, 'customer_name', '') or ''
        hi      = f'Hi {name}' if name else 'Hi there'
        call    = _call_phone(apt)

        subject = 'Quick last check' + (f', {name}' if name else '')

        body = (
            f'<p>{hi},</p>'
            f'<p>I won\'t keep emailing — promise. Just wanted to give you '
            f'one last easy way to pick this up.</p>'
            f'<p>If the timing is right, reply with a day that suits you '
            f'(morning or afternoon) and I\'ll book the on-site visit at no cost.</p>'
            f'<p>If the timing\'s off, no problem at all — just reply "later" '
            f'and we\'ll quietly close this out. You can always come back to '
            f'us when you\'re ready.</p>'
            f'{_contact_buttons(call)}'
            f'<p>Takudzwa<br>HomeBase Plumbers<br>+{call}</p>'
        )

        html = (
            '<!DOCTYPE html><html><body>'
            f'{body}'
            '</body></html>'
        )

        ok = _send(apt, subject, html)
        if ok:
            logger.info("Delay last-check email sent — apt %s", apt.pk)
        return ok
    except Exception:
        logger.exception("send_delay_last_check_email failed — apt %s", apt.pk)
        return False


def build_plumber_booking_email_html(
    *, customer_name, customer_phone_digits, datetime_str, service,
    area=None, property_type=None, timeline=None, plan_status=None,
    view_url=None,
):
    """
    HTML body for the plumber new-booking notification email.

    Shows the booking details and Call/WhatsApp buttons that dial the CUSTOMER
    (via _customer_contact_buttons), so the plumber can reach them in one tap.
    Returns a fully wrapped HTML string.
    """
    name = customer_name or "Unknown"
    digits = _clean_phone(customer_phone_digits)

    rows = [('📅 Date/Time', datetime_str), ('🔧 Service', service)]
    if area:          rows.append(('📍 Area', area))
    if property_type: rows.append(('🏠 Property', property_type))
    if timeline:      rows.append(('⏰ Timeline', timeline))
    if plan_status:   rows.append(('📐 Plan', plan_status))
    if digits:        rows.append(('📞 Phone', f'+{digits}'))

    detail_rows = ''.join(
        f'<p style="margin:3px 0;color:#444;font-size:14px;">'
        f'<strong>{label}:</strong> {value}</p>'
        for label, value in rows
    )
    card = (
        '<div style="border-left:4px solid #25D366;padding:12px 16px;'
        'margin:16px 0;background:#f9f9f9;border-radius:0 6px 6px 0;">'
        f'{detail_rows}'
        '</div>'
    )

    view_link = (
        f'<p style="margin:16px 0 0;font-size:14px;">'
        f'<a href="{view_url}" style="color:#1a9e4a;">View full details →</a></p>'
        if view_url else ''
    )

    body = (
        f'<p>Hi Team,</p>'
        f'<p>New appointment booked by <strong>{name}</strong>.</p>'
        f'{card}'
        f'{_customer_contact_buttons(digits)}'
        f'{view_link}'
    )
    return _wrap(body)


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
