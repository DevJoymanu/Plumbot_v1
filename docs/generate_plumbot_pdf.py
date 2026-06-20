"""
Plumbot – Full Feature & Funnel PDF Generator
Run: python docs/generate_plumbot_pdf.py
Output: docs/Plumbot_Overview.pdf
"""

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm, cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable, KeepTogether
)
from reportlab.platypus.flowables import Flowable
from reportlab.graphics.shapes import (
    Drawing, Rect, Circle, String, Line,
    Polygon, Group, Path
)
from reportlab.graphics import renderPDF
from reportlab.pdfgen import canvas as pdfcanvas
import os

# ── Colour Palette ─────────────────────────────────────────────────────────────
BRAND_GREEN   = colors.HexColor("#25D366")   # WhatsApp green
BRAND_DARK    = colors.HexColor("#128C7E")   # WhatsApp teal
BRAND_LIGHT   = colors.HexColor("#DCF8C6")   # WhatsApp light bubble
ACCENT_ORANGE = colors.HexColor("#FF6B35")   # CTA accent
ACCENT_BLUE   = colors.HexColor("#007AFF")   # iOS-style blue
FUNNEL_1      = colors.HexColor("#4CAF50")
FUNNEL_2      = colors.HexColor("#2196F3")
FUNNEL_3      = colors.HexColor("#FF9800")
FUNNEL_4      = colors.HexColor("#9C27B0")
FUNNEL_5      = colors.HexColor("#F44336")
FUNNEL_6      = colors.HexColor("#00BCD4")
FUNNEL_7      = colors.HexColor("#FFD700")
DARK_BG       = colors.HexColor("#1F2937")
LIGHT_GREY    = colors.HexColor("#F3F4F6")
MID_GREY      = colors.HexColor("#9CA3AF")
TEXT_DARK     = colors.HexColor("#111827")
TEXT_BODY     = colors.HexColor("#374151")

W, H = A4  # 595 x 842 pts

# ── Styles ─────────────────────────────────────────────────────────────────────
styles = getSampleStyleSheet()

def make_style(name, parent="Normal", **kwargs):
    return ParagraphStyle(name, parent=styles[parent], **kwargs)

COVER_TITLE   = make_style("CoverTitle",   fontSize=34, leading=40,  textColor=colors.white,      alignment=TA_CENTER, fontName="Helvetica-Bold")
COVER_SUB     = make_style("CoverSub",     fontSize=16, leading=22,  textColor=BRAND_LIGHT,       alignment=TA_CENTER, fontName="Helvetica")
COVER_DATE    = make_style("CoverDate",    fontSize=11, leading=16,  textColor=MID_GREY,           alignment=TA_CENTER, fontName="Helvetica")
H1            = make_style("H1",           fontSize=22, leading=28,  textColor=BRAND_DARK,        spaceAfter=6,  fontName="Helvetica-Bold")
H2            = make_style("H2",           fontSize=15, leading=20,  textColor=BRAND_DARK,        spaceAfter=4,  fontName="Helvetica-Bold")
H3            = make_style("H3",           fontSize=12, leading=16,  textColor=TEXT_DARK,         spaceAfter=3,  fontName="Helvetica-Bold")
BODY          = make_style("BODY",         fontSize=10, leading=15,  textColor=TEXT_BODY,         spaceAfter=4,  fontName="Helvetica", alignment=TA_JUSTIFY)
BODY_LEFT     = make_style("BODY_LEFT",    fontSize=10, leading=15,  textColor=TEXT_BODY,         spaceAfter=4,  fontName="Helvetica")
BULLET        = make_style("BULLET",       fontSize=10, leading=14,  textColor=TEXT_BODY,         spaceAfter=2,  fontName="Helvetica", leftIndent=14, firstLineIndent=-10)
CAPTION       = make_style("CAPTION",      fontSize=9,  leading=12,  textColor=MID_GREY,           alignment=TA_CENTER, fontName="Helvetica-Oblique")
TABLE_HEADER  = make_style("TH",           fontSize=10, leading=14,  textColor=colors.white,      fontName="Helvetica-Bold", alignment=TA_CENTER)
TABLE_CELL    = make_style("TC",           fontSize=9,  leading=13,  textColor=TEXT_BODY,         fontName="Helvetica")
BADGE         = make_style("BADGE",        fontSize=9,  leading=12,  textColor=colors.white,      fontName="Helvetica-Bold", alignment=TA_CENTER)
GAIN_STYLE    = make_style("GAIN",         fontSize=9,  leading=13,  textColor=FUNNEL_1,          fontName="Helvetica-Bold")
CHAT_BOT      = make_style("ChatBot",      fontSize=9,  leading=13,  textColor=TEXT_DARK,         fontName="Helvetica",      leftIndent=8)
CHAT_USER     = make_style("ChatUser",     fontSize=9,  leading=13,  textColor=TEXT_DARK,         fontName="Helvetica",      rightIndent=8, alignment=2)  # right

def b(text): return f"<b>{text}</b>"
def g(text): return f'<font color="#128C7E"><b>{text}</b></font>'
def o(text): return f'<font color="#FF6B35"><b>{text}</b></font>'

# ── Custom Flowables ────────────────────────────────────────────────────────────

class ColorBar(Flowable):
    def __init__(self, color, height=4, width=None):
        Flowable.__init__(self)
        self._color  = color
        self._height = height
        self._width  = width

    def wrap(self, availW, availH):
        self._width = self._width or availW
        return self._width, self._height

    def draw(self):
        self.canv.setFillColor(self._color)
        self.canv.rect(0, 0, self._width, self._height, fill=1, stroke=0)


class SectionHeader(Flowable):
    def __init__(self, text, color=BRAND_DARK, width=None):
        Flowable.__init__(self)
        self._text  = text
        self._color = color
        self._width = width
        self._height = 28

    def wrap(self, availW, availH):
        self._width = self._width or availW
        return self._width, self._height

    def draw(self):
        c = self.canv
        c.setFillColor(self._color)
        c.roundRect(0, 0, self._width, self._height, 4, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(10, 8, self._text)


class WhatsAppBubble(Flowable):
    """Renders a fake WhatsApp chat exchange."""
    def __init__(self, messages, width=None):
        Flowable.__init__(self)
        self._messages = messages  # list of (sender, text) — sender: "bot"|"user"
        self._width = width
        self._height = 0

    def wrap(self, availW, availH):
        self._width = self._width or availW
        # estimate height: each message ~22pt + 4pt gap
        self._height = sum(max(22, len(m[1])//55 * 13 + 22) for m in self._messages) + 20
        return self._width, self._height

    def draw(self):
        c = self.canv
        # phone frame
        c.setFillColor(colors.HexColor("#ECE5DD"))
        c.roundRect(0, 0, self._width, self._height, 6, fill=1, stroke=0)
        # header bar
        c.setFillColor(BRAND_DARK)
        c.rect(0, self._height-22, self._width, 22, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(8, self._height-15, "Homebase Plumbers  •  WhatsApp")

        y = self._height - 30
        for sender, text in self._messages:
            lines = []
            words = text.split()
            line = ""
            for w in words:
                test = (line + " " + w).strip()
                if len(test) > 45:
                    lines.append(line)
                    line = w
                else:
                    line = test
            if line:
                lines.append(line)
            box_h = len(lines)*13 + 10
            box_w = min(self._width * 0.72, max(80, max(len(l) for l in lines)*6.2 + 16))

            if sender == "bot":
                bx = 6
                c.setFillColor(colors.white)
            else:
                bx = self._width - box_w - 6
                c.setFillColor(BRAND_LIGHT)

            c.roundRect(bx, y - box_h, box_w, box_h, 4, fill=1, stroke=0)
            c.setFillColor(TEXT_DARK)
            c.setFont("Helvetica", 8)
            for i, line in enumerate(lines):
                c.drawString(bx + 6, y - 14 - i*13, line)

            y -= box_h + 6


class FunnelDiagram(Flowable):
    """Trapezoidal funnel with stage labels."""
    def __init__(self, stages, width=None, height=340):
        Flowable.__init__(self)
        self._stages = stages  # list of (label, sublabel, color)
        self._width  = width
        self._height = height

    def wrap(self, availW, availH):
        self._width = self._width or availW
        return self._width, self._height

    def draw(self):
        c   = self.canv
        n   = len(self._stages)
        h   = self._height / n
        top_w  = self._width * 0.88
        bot_w  = self._width * 0.30
        cx  = self._width / 2

        for i, (label, sublabel, clr) in enumerate(self._stages):
            # trapezoid widths
            w_top = top_w - (top_w - bot_w) * (i / n)
            w_bot = top_w - (top_w - bot_w) * ((i+1) / n)
            y_top = self._height - h * i
            y_bot = self._height - h * (i+1)
            x_top_l = cx - w_top/2
            x_bot_l = cx - w_bot/2

            # draw trapezoid
            p = c.beginPath()
            p.moveTo(x_top_l, y_top)
            p.lineTo(x_top_l + w_top, y_top)
            p.lineTo(x_bot_l + w_bot, y_bot)
            p.lineTo(x_bot_l, y_bot)
            p.close()
            c.setFillColor(clr)
            c.setStrokeColor(colors.white)
            c.setLineWidth(1.5)
            c.drawPath(p, fill=1, stroke=1)

            # label
            mid_y  = (y_top + y_bot) / 2
            mid_w  = (w_top + w_bot) / 2
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 10)
            c.drawCentredString(cx, mid_y + 3, label)
            if sublabel:
                c.setFont("Helvetica", 7.5)
                c.drawCentredString(cx, mid_y - 8, sublabel)


class FlowChart(Flowable):
    """Simple top-down flow chart with boxes and arrows."""
    def __init__(self, steps, width=None):
        Flowable.__init__(self)
        self._steps = steps  # list of (text, color) or (text, color, "decision")
        self._width = width
        self._box_h = 28
        self._gap   = 18
        n = len(steps)
        self._height = n * self._box_h + (n-1)*self._gap + 20

    def wrap(self, availW, availH):
        self._width = self._width or availW
        return self._width, self._height

    def draw(self):
        c    = self.canv
        bw   = self._width * 0.72
        cx   = self._width / 2
        bx   = cx - bw/2
        bh   = self._box_h
        gap  = self._gap
        y    = self._height - 10

        for i, step in enumerate(self._steps):
            if len(step) == 3:
                text, clr, shape = step
            else:
                text, clr = step
                shape = "rect"

            y_top = y - bh
            mid_y = y - bh/2

            if shape == "decision":
                # diamond
                half_w = bw/2
                half_h = bh/2
                pts = [cx, y,  cx+half_w, mid_y,  cx, y_top,  cx-half_w, mid_y]
                p = c.beginPath()
                p.moveTo(pts[0], pts[1])
                p.lineTo(pts[2], pts[3])
                p.lineTo(pts[4], pts[5])
                p.lineTo(pts[6], pts[7])
                p.close()
                c.setFillColor(clr)
                c.setStrokeColor(colors.white)
                c.drawPath(p, fill=1, stroke=1)
            else:
                c.setFillColor(clr)
                c.setStrokeColor(colors.white)
                c.roundRect(bx, y_top, bw, bh, 5, fill=1, stroke=1)

            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 8.5)
            c.drawCentredString(cx, mid_y - 4, text)

            # arrow down (not last)
            if i < len(self._steps)-1:
                ax = cx
                ay_top = y_top - 2
                ay_bot = y_top - gap + 2
                c.setStrokeColor(BRAND_DARK)
                c.setLineWidth(1.5)
                c.line(ax, ay_top, ax, ay_bot+5)
                # arrowhead
                c.setFillColor(BRAND_DARK)
                p2 = c.beginPath()
                p2.moveTo(ax, ay_bot)
                p2.lineTo(ax-4, ay_bot+7)
                p2.lineTo(ax+4, ay_bot+7)
                p2.close()
                c.drawPath(p2, fill=1, stroke=0)

            y -= bh + gap


# ── Page Template ───────────────────────────────────────────────────────────────

def draw_cover(canv, doc):
    """Draws the full cover page directly onto the canvas."""
    canv.saveState()
    c = canv
    c.setFillColor(DARK_BG)
    c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColor(BRAND_GREEN)
    c.rect(0, H*0.52, W, 6, fill=1, stroke=0)
    c.rect(0, H*0.52-8, W, 2, fill=1, stroke=0)
    c.setFillColor(BRAND_GREEN)
    c.circle(W/2, H*0.70, 45, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 38)
    c.drawCentredString(W/2, H*0.70-14, "P")
    c.setFont("Helvetica", 11)
    c.drawCentredString(W/2, H*0.70-28, "PLUMBOT")
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 30)
    c.drawCentredString(W/2, H*0.46, "PLUMBOT")
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(W/2, H*0.41, "WhatsApp Sales & Booking Automation")
    c.setStrokeColor(BRAND_GREEN)
    c.setLineWidth(2)
    c.line(W*0.25, H*0.385, W*0.75, H*0.385)
    c.setFillColor(BRAND_LIGHT)
    c.setFont("Helvetica", 13)
    c.drawCentredString(W/2, H*0.355, "For Homebase Plumbers  •  Harare, Zimbabwe")
    badges = ["AI-Powered", "WhatsApp Native", "24/7 Automated", "Lead Scoring"]
    bw2 = 80; gap2 = 10
    total2 = len(badges)*bw2 + (len(badges)-1)*gap2
    sx2 = (W - total2)/2
    for i2, badge in enumerate(badges):
        bx2 = sx2 + i2*(bw2+gap2)
        c.setFillColor(BRAND_GREEN)
        c.roundRect(bx2, H*0.30, bw2, 20, 4, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(bx2+bw2/2, H*0.30+6, badge)
    c.setFillColor(colors.HexColor("#374151"))
    c.roundRect(W*0.08, H*0.18, W*0.84, 60, 6, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica", 10)
    desc_lines = [
        "Plumbot is a fully-automated, AI-powered WhatsApp assistant that qualifies leads,",
        "books appointments, follows up with customers, and drives Google reviews --",
        "so your plumbers focus on doing the work, not chasing it."
    ]
    for i3, line3 in enumerate(desc_lines):
        c.drawCentredString(W/2, H*0.18+42-i3*14, line3)
    c.setFillColor(MID_GREY)
    c.setFont("Helvetica", 9)
    c.drawCentredString(W/2, H*0.12, "May 2026  •  Confidential  •  Prepared for Homebase Plumbers")
    canv.restoreState()


def on_page(canv, doc):
    """Header/footer on every page except cover."""
    if doc.page == 1:
        draw_cover(canv, doc)
        return
    canv.saveState()
    # top bar
    canv.setFillColor(BRAND_DARK)
    canv.rect(0, H-28, W, 28, fill=1, stroke=0)
    canv.setFillColor(colors.white)
    canv.setFont("Helvetica-Bold", 9)
    canv.drawString(18, H-19, "PLUMBOT")
    canv.setFont("Helvetica", 9)
    canv.drawRightString(W-18, H-19, "WhatsApp Sales & Booking Automation  •  Homebase Plumbers")
    # bottom bar
    canv.setFillColor(LIGHT_GREY)
    canv.rect(0, 0, W, 18, fill=1, stroke=0)
    canv.setFillColor(MID_GREY)
    canv.setFont("Helvetica", 7.5)
    canv.drawString(18, 5, "Confidential  •  Homebase Plumbers  •  Harare, Zimbabwe")
    canv.drawRightString(W-18, 5, f"Page {doc.page}")
    canv.restoreState()


# ── Build Document ──────────────────────────────────────────────────────────────

def build():
    out_path = os.path.join(os.path.dirname(__file__), "Plumbot_Overview.pdf")
    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=14*mm, bottomMargin=14*mm,
        onPage=on_page
    )
    story = []
    PW = W - 36*mm  # printable width

    # PAGE 1 is drawn entirely via draw_cover() in on_page() callback.
    # A PageBreak here forces the story content to start on page 2.
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════════
    # PAGE 2 — EXECUTIVE SUMMARY
    # ════════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 10))
    story.append(SectionHeader("  EXECUTIVE SUMMARY", DARK_BG, PW))
    story.append(Spacer(1, 10))

    story.append(Paragraph(
        "Plumbot is Homebase Plumbers' always-on WhatsApp sales assistant. It handles every stage of "
        "the customer journey — from the first \"Hi\" message right through to a completed job and a "
        "5-star Google review — without requiring a single manual reply from your team.", BODY))
    story.append(Spacer(1, 6))

    # Key stats table
    stats = [
        [Paragraph("<b>Metric</b>", TABLE_HEADER), Paragraph("<b>Without Plumbot</b>", TABLE_HEADER), Paragraph("<b>With Plumbot</b>", TABLE_HEADER)],
        ["Response time to new leads",    "Hours (or missed)",    "~5 min, 24/7 (human-paced)"],
        ["Follow-up attempts per lead",   "0–1 (manual)",         "Up to 4 automated"],
        ["Lead qualification time",       "10–20 min per lead",   "~2 min (automated)"],
        ["Bookings missed after hours",   "Most",                 "Zero (always on)"],
        ["Languages supported",           "English only",         "English + Shona"],
        ["Google reviews solicited",      "Rarely / manually",    "Every completed job"],
    ]
    t = Table(stats, colWidths=[PW*0.38, PW*0.31, PW*0.31])
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1,0), BRAND_DARK),
        ("TEXTCOLOR",    (0,0), (-1,0), colors.white),
        ("FONTNAME",     (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",     (0,0), (-1,-1), 9),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[LIGHT_GREY, colors.white]),
        ("ALIGN",        (1,1), (-1,-1), "CENTER"),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("GRID",         (0,0), (-1,-1), 0.4, MID_GREY),
        ("TOPPADDING",   (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0), (-1,-1), 5),
        ("LEFTPADDING",  (0,0), (-1,-1), 6),
    ]))
    story.append(t)
    story.append(Spacer(1, 10))

    story.append(Paragraph(b("How the ROI adds up:"), H2))
    roi_items = [
        ("Every missed after-hours lead = lost revenue.", "Plumbot replies within 5 minutes, 24/7 — at a natural human pace."),
        ("A plumber spending 30 min/day on WhatsApp = 182 hours/year.", "Plumbot handles it automatically."),
        ("Only 1 in 5 businesses actively ask for Google reviews.", "Plumbot asks every time, automatically."),
        ("Most trades businesses lose leads due to slow follow-up.", "Plumbot follows up within 2 hours, 4 times."),
    ]
    for pain, gain in roi_items:
        story.append(Paragraph(f"<font color='#F44336'>✗  {pain}</font>  →  "
                                f"<font color='#128C7E'><b>✓  {gain}</b></font>", BULLET))
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════════
    # PAGE 3 — FEATURES & BENEFITS (Part 1)
    # ════════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 6))
    story.append(SectionHeader("  FEATURES & BENEFITS", BRAND_DARK, PW))
    story.append(Spacer(1, 8))

    features = [
        {
            "icon": "01",
            "title": "24/7 WhatsApp Response — Human-Paced (5-Min Timer)",
            "what": "Every customer WhatsApp message is answered within 5 minutes, any time of day or night. A deliberate delay timer emulates a real person typing and thinking — not a bot firing back in milliseconds.",
            "benefit": "Customers experience a natural, trusted interaction instead of an obviously robotic one. You never miss a lead, but you also never come across as a cold machine.",
            "gain": "30–40% more leads captured from after-hours enquiries. Higher trust = higher conversion vs instant-bot competitors.",
            "color": FUNNEL_1,
        },
        {
            "icon": "02",
            "title": "AI-Powered Intent Classification",
            "what": "DeepSeek AI reads every customer message and instantly classifies what they want: bathroom reno, geyser repair, new plumbing, pricing query, or something out of scope.",
            "benefit": "The right response is delivered every time. No wrong templates, no confused replies. The bot understands context, not just keywords.",
            "gain": "Eliminates misrouted enquiries and reduces customer frustration.",
            "color": FUNNEL_2,
        },
        {
            "icon": "03",
            "title": "4-Stage Sales Qualification (Hormozi Framework)",
            "what": "Plumbot guides each customer through Value → Price → Qualification → Close — the same proven framework used by top sales teams, automated.",
            "benefit": "By the time a customer books, they already understand your pricing, trust your expertise, and are committed. No price objections on the day.",
            "gain": "Higher quality leads, fewer no-shows, and shorter sales cycles.",
            "color": FUNNEL_3,
        },
        {
            "icon": "04",
            "title": "Lead Scoring — Hot/Warm/Cold",
            "what": "Every lead is automatically scored 0–100 and classified as Cold, Warm, Hot, or Very Hot based on how much booking information has been collected.",
            "benefit": "Your plumber knows at a glance which leads need immediate attention vs which are still warming up. No more guessing who to call back first.",
            "gain": "Prioritised sales effort = more conversions per hour of plumber time.",
            "color": FUNNEL_4,
        },
        {
            "icon": "05",
            "title": "Automated Follow-Up Sequences",
            "what": "If a customer goes quiet, Plumbot automatically sends up to 4 contextual follow-up messages over 24 hours — rephrased each time using AI so they never feel spammy.",
            "benefit": "Leads that would have been lost to silence are re-engaged automatically. No manual chasing required.",
            "gain": "Industry data shows follow-up can recover 20–35% of otherwise lost leads.",
            "color": FUNNEL_5,
        },
        {
            "icon": "06",
            "title": "Smart Pricing Communication",
            "what": "Plumbot shares pricing overviews and product-specific quotes at the right moment in the conversation. It remembers which pricing it has already sent and never repeats itself.",
            "benefit": "Customers get clear, upfront pricing without having to ask twice. Removes the #1 friction point in the sales process.",
            "gain": "Faster decision-making, fewer price-related drop-offs.",
            "color": FUNNEL_6,
        },
    ]

    for feat in features:
        row_data = [
            [
                Paragraph(f'<font color="white"><b>{feat["icon"]}</b></font>', make_style(f"ic{feat['icon']}", fontSize=13, alignment=TA_CENTER, textColor=colors.white)),
            ],
            [
                Paragraph(b(feat["title"]), H3),
                Paragraph(feat["what"],  TABLE_CELL),
                Paragraph(f'<font color="#128C7E"><b>Client Benefit: </b></font>' + feat["benefit"], TABLE_CELL),
                Paragraph(f'<font color="#FF6B35"><b>Potential Gain: </b></font>' + feat["gain"], TABLE_CELL),
            ]
        ]

        inner = Table(
            [[Paragraph(b(feat["title"]), H3)],
             [Paragraph(feat["what"], TABLE_CELL)],
             [Paragraph(f'<font color="#128C7E"><b>Client Benefit: </b></font>' + feat["benefit"], TABLE_CELL)],
             [Paragraph(f'<font color="#FF6B35"><b>⬆ Potential Gain: </b></font>' + feat["gain"], TABLE_CELL)],
            ],
            colWidths=[PW - 36]
        )
        inner.setStyle(TableStyle([
            ("TOPPADDING",   (0,0), (-1,-1), 2),
            ("BOTTOMPADDING",(0,0), (-1,-1), 2),
            ("LEFTPADDING",  (0,0), (-1,-1), 0),
        ]))

        num_cell = Table([[Paragraph(feat["icon"], make_style(f"n{feat['icon']}", fontSize=12, fontName="Helvetica-Bold", textColor=colors.white, alignment=TA_CENTER))]],
                          colWidths=[28], rowHeights=[28])
        num_cell.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (0,0), feat["color"]),
            ("VALIGN",       (0,0), (0,0), "MIDDLE"),
            ("TOPPADDING",   (0,0), (0,0), 4),
            ("BOTTOMPADDING",(0,0), (0,0), 4),
        ]))

        card = Table([[num_cell, inner]], colWidths=[36, PW-36])
        card.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,-1), LIGHT_GREY),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",   (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0), (-1,-1), 6),
            ("LEFTPADDING",  (1,0), (1,0), 8),
            ("LINEBELOW",    (0,0), (-1,-1), 0.5, MID_GREY),
        ]))
        story.append(KeepTogether([card, Spacer(1, 5)]))

    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════════
    # PAGE 4 — FEATURES & BENEFITS (Part 2)
    # ════════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 6))
    story.append(SectionHeader("  FEATURES & BENEFITS  (continued)", BRAND_DARK, PW))
    story.append(Spacer(1, 8))

    features2 = [
        {
            "icon": "07",
            "title": "Bilingual Support — English & Shona",
            "what": "Plumbot detects whether the customer is writing in English, Shona, or a mix of both, and responds naturally in the same language — using authentic Zimbabwean Shona.",
            "benefit": "Shona-speaking customers feel respected and understood. Conversion rates among this segment improve significantly.",
            "gain": "Expands your addressable market to the full Harare population.",
            "color": FUNNEL_1,
        },
        {
            "icon": "08",
            "title": "Plan Upload & Blueprint Handling",
            "what": "When a customer has building plans or blueprints, Plumbot prompts them to upload the files directly via WhatsApp. Files are stored to cloud and the plumber is notified instantly.",
            "benefit": "Plumber receives all project documents before the site visit — saving time and arriving prepared.",
            "gain": "Fewer repeat site visits. More accurate quotes upfront.",
            "color": FUNNEL_2,
        },
        {
            "icon": "09",
            "title": "Google Calendar Appointment Booking",
            "what": "Once a customer is ready to book, Plumbot checks real-time availability, proposes times, and books the appointment directly into Google Calendar — with conflict detection.",
            "benefit": "No double-bookings. The plumber's calendar is always accurate and up-to-date.",
            "gain": "Eliminates scheduling errors and the admin overhead of manual booking.",
            "color": FUNNEL_3,
        },
        {
            "icon": "10",
            "title": "Appointment Reminders (3-Stage)",
            "what": "Plumbot sends three WhatsApp reminders: 1 day before, the morning of, and 2 hours before the appointment.",
            "benefit": "Customers show up on time, prepared. Plumber's time is not wasted on no-shows.",
            "gain": "No-show rates typically drop 40–60% with automated reminders.",
            "color": FUNNEL_4,
        },
        {
            "icon": "11",
            "title": "Dashboard & Analytics",
            "what": "A web-based dashboard gives the team a real-time view of all leads, appointments, lead temperatures, follow-up pipeline, and booking conversion rates.",
            "benefit": "Full visibility into the sales pipeline at a glance. Know exactly which leads need attention and why.",
            "gain": "Data-driven decisions instead of gut feel.",
            "color": FUNNEL_5,
        },
        {
            "icon": "12",
            "title": "NEW: Automated Google Review Request (Email)",
            "what": "When a job is marked as completed, the system automatically sends a personalised email to the customer with a direct link to leave a Google review for Homebase Plumbers.",
            "benefit": "You get Google reviews passively, without ever having to ask manually. Reviews are solicited at the perfect moment — right when the customer is happiest.",
            "gain": "More 5-star reviews → higher Google Maps ranking → more organic leads. Every review compounds.",
            "color": ACCENT_ORANGE,
        },
    ]

    for feat in features2:
        inner = Table(
            [[Paragraph(b(feat["title"]), H3)],
             [Paragraph(feat["what"], TABLE_CELL)],
             [Paragraph(f'<font color="#128C7E"><b>Client Benefit: </b></font>' + feat["benefit"], TABLE_CELL)],
             [Paragraph(f'<font color="#FF6B35"><b>⬆ Potential Gain: </b></font>' + feat["gain"], TABLE_CELL)],
            ],
            colWidths=[PW - 36]
        )
        inner.setStyle(TableStyle([
            ("TOPPADDING",   (0,0), (-1,-1), 2),
            ("BOTTOMPADDING",(0,0), (-1,-1), 2),
            ("LEFTPADDING",  (0,0), (-1,-1), 0),
        ]))
        num_cell = Table([[Paragraph(feat["icon"], make_style(f"n2{feat['icon']}", fontSize=12, fontName="Helvetica-Bold", textColor=colors.white, alignment=TA_CENTER))]],
                          colWidths=[28], rowHeights=[28])
        num_cell.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (0,0), feat["color"]),
            ("VALIGN",       (0,0), (0,0), "MIDDLE"),
            ("TOPPADDING",   (0,0), (0,0), 4),
            ("BOTTOMPADDING",(0,0), (0,0), 4),
        ]))
        card = Table([[num_cell, inner]], colWidths=[36, PW-36])
        card.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,-1), LIGHT_GREY),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",   (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0), (-1,-1), 6),
            ("LEFTPADDING",  (1,0), (1,0), 8),
            ("LINEBELOW",    (0,0), (-1,-1), 0.5, MID_GREY),
        ]))
        story.append(KeepTogether([card, Spacer(1, 5)]))

    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════════
    # PAGE 5 — COMPLETE CUSTOMER FUNNEL
    # ════════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 6))
    story.append(SectionHeader("  COMPLETE CUSTOMER FUNNEL — WhatsApp to Google Review", DARK_BG, PW))
    story.append(Spacer(1, 10))

    funnel_stages = [
        ("AWARENESS", "Customer discovers Homebase via Google, referral, or social media", FUNNEL_1),
        ("FIRST CONTACT", "Customer sends WhatsApp message → Plumbot responds within ~5 min (human-paced)", FUNNEL_2),
        ("QUALIFICATION", "AI classifies service type, area, plans → Lead score assigned", FUNNEL_3),
        ("PRICING", "Plumbot shares transparent pricing overview → trust established", FUNNEL_4),
        ("BOOKING", "Customer selects time → Google Calendar appointment created", FUNNEL_5),
        ("SITE VISIT / JOB", "Plumber attends, completes work → Job marked complete", FUNNEL_6),
        ("GOOGLE REVIEW", "Automated email sent → Customer leaves 5-star review", FUNNEL_7),
    ]
    story.append(FunnelDiagram(funnel_stages, PW, height=300))
    story.append(Spacer(1, 8))

    story.append(Paragraph(b("What happens at each stage:"), H2))
    funnel_detail = [
        [Paragraph("<b>Stage</b>", TABLE_HEADER), Paragraph("<b>Plumbot Action</b>", TABLE_HEADER), Paragraph("<b>Customer Experience</b>", TABLE_HEADER), Paragraph("<b>Client Benefit</b>", TABLE_HEADER)],
        ["1. Awareness",      "N/A — pre-Plumbot",                              "Sees listing / ad / referral",         "Strong first impression"],
        ["2. First Contact",  "AI reply within ~5 min (human-paced timer)",     "Feels like a real person replied",     "Lead captured + trust established"],
        ["3. Qualification",  "Extracts service, area, availability",           "Feels heard and understood",           "Qualified pipeline"],
        ["4. Pricing",        "Sends transparent pricing overview",             "Makes informed decision",              "Fewer price surprises"],
        ["5. Booking",        "Checks calendar, books, sends confirmation",     "Smooth, professional booking",         "No double-bookings"],
        ["6. Job",            "Sends 3 reminders; plumber arrives prepared",    "Reminded, no no-shows",                "Efficient job delivery"],
        ["7. Google Review",  "Auto-email with review link after job complete", "Easy one-click review request",        "Organic lead engine"],
    ]
    ft = Table(funnel_detail, colWidths=[PW*0.18, PW*0.27, PW*0.27, PW*0.28])
    ft.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), DARK_BG),
        ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
        ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 8.5),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[LIGHT_GREY, colors.white]),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("GRID",          (0,0), (-1,-1), 0.4, MID_GREY),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 5),
    ]))
    story.append(ft)
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════════
    # PAGE 6 — CONVERSATION FLOW DIAGRAM
    # ════════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 6))
    story.append(SectionHeader("  CONVERSATION FLOW DIAGRAM", BRAND_DARK, PW))
    story.append(Spacer(1, 8))

    flow_steps = [
        ("Customer sends WhatsApp message",              BRAND_GREEN),
        ("WAMID deduplication check",                   BRAND_DARK),
        ("5-minute human-response timer starts",        colors.HexColor("#795548")),
        ("Exit / delay signal detection",               colors.HexColor("#607D8B")),
        ("AI Intent Classification (DeepSeek)",         FUNNEL_2),
        ("Is it in scope?",                             colors.HexColor("#FF9800"), "decision"),
        ("Out-of-scope handler → graceful redirect",    colors.HexColor("#F44336")),
        ("Lead scoring & stage detection",              FUNNEL_3),
        ("Stage: VALUE → share portfolio / intro",      FUNNEL_1),
        ("Stage: PRICE → send pricing overview",        FUNNEL_4),
        ("Stage: QUALIFY → ask targeted questions",     FUNNEL_2),
        ("Stage: CLOSE → propose booking time",         FUNNEL_5),
        ("Availability check (Google Calendar)",        BRAND_DARK),
        ("Appointment confirmed → reminders scheduled", FUNNEL_6),
        ("Job completed → Google Review email sent",    FUNNEL_7),
    ]
    # Split into two columns visually — draw as single column with compact spacing
    story.append(FlowChart(flow_steps, PW))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Note: Every outbound message is delayed by a 5-minute human-response timer before sending, "
        "so the conversation feels natural rather than robotic. "
        "If the customer goes silent at any stage, the automated follow-up engine activates "
        "and sends up to 4 contextually rephrased messages within 24 hours.", CAPTION))
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════════
    # PAGE 7 — LEAD SCORING SYSTEM
    # ════════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 6))
    story.append(SectionHeader("  LEAD SCORING SYSTEM", BRAND_DARK, PW))
    story.append(Spacer(1, 10))

    story.append(Paragraph(
        "Every lead is scored in real-time based on how much booking information has been collected. "
        "Plumbot tracks four key data points:", BODY))
    story.append(Spacer(1, 6))

    scoring_cols = [
        [Paragraph("<b>Data Point</b>", TABLE_HEADER), Paragraph("<b>Points</b>", TABLE_HEADER), Paragraph("<b>How Collected</b>", TABLE_HEADER)],
        ["Service Type (bathroom, kitchen, geyser…)", "25 pts", "AI classifies from first message"],
        ["Has Plans / Needs Site Visit",              "25 pts", "Customer answers plan question"],
        ["Customer Area / Suburb",                    "25 pts", "Extracted from customer message"],
        ["Scheduled Appointment Date/Time",           "25 pts", "Appointment booked via Plumbot"],
    ]
    st = Table(scoring_cols, colWidths=[PW*0.50, PW*0.15, PW*0.35])
    st.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), BRAND_DARK),
        ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
        ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 9),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[LIGHT_GREY, colors.white]),
        ("ALIGN",         (1,0), (1,-1), "CENTER"),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("GRID",          (0,0), (-1,-1), 0.4, MID_GREY),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
    ]))
    story.append(st)
    story.append(Spacer(1, 12))

    # Temperature gauge visual
    class TempGauge(Flowable):
        def wrap(self, aw, ah): return PW, 80
        def draw(self):
            c = self.canv
            bands = [
                (0, 25,  "COLD",      "0–25 pts",  colors.HexColor("#64B5F6")),
                (25, 50, "WARM",      "26–50 pts", colors.HexColor("#FFB74D")),
                (50, 75, "HOT",       "51–74 pts", colors.HexColor("#EF5350")),
                (75, 100,"VERY HOT",  "75–100 pts",colors.HexColor("#B71C1C")),
            ]
            bw = PW / 4
            for i, (lo, hi, label, pts, clr) in enumerate(bands):
                bx = i * bw
                c.setFillColor(clr)
                c.roundRect(bx+2, 30, bw-4, 32, 4, fill=1, stroke=0)
                c.setFillColor(colors.white)
                c.setFont("Helvetica-Bold", 11)
                c.drawCentredString(bx + bw/2, 49, label)
                c.setFont("Helvetica", 8)
                c.drawCentredString(bx + bw/2, 37, pts)
                # bar below
                c.setFillColor(clr)
                c.rect(bx+2, 18, bw-4, 8, fill=1, stroke=0)
                c.setFillColor(TEXT_DARK)
                c.setFont("Helvetica", 7)
                if label == "COLD":
                    c.drawCentredString(bx + bw/2, 5, "No priority alerts")
                elif label == "WARM":
                    c.drawCentredString(bx + bw/2, 5, "Standard follow-up")
                elif label == "HOT":
                    c.drawCentredString(bx + bw/2, 5, "Priority alert sent")
                else:
                    c.drawCentredString(bx + bw/2, 5, "Booked ✓")

    story.append(TempGauge())
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "<b>Client Benefit:</b> Your plumber sees exactly which leads are hot and which need nurturing — "
        "without reading a single WhatsApp message.", BODY))
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════════
    # PAGE 8 — FOLLOW-UP AUTOMATION TIMELINE
    # ════════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 6))
    story.append(SectionHeader("  FOLLOW-UP AUTOMATION TIMELINE", BRAND_DARK, PW))
    story.append(Spacer(1, 10))

    story.append(Paragraph(
        "When a customer goes silent, Plumbot automatically re-engages them up to 4 times within 24 hours. "
        "Each follow-up is AI-rephrased so it never feels like a copied message. "
        "Contact hours are respected (8 AM – 9 PM).", BODY))
    story.append(Spacer(1, 8))

    class FollowUpTimeline(Flowable):
        def wrap(self, aw, ah): return PW, 200
        def draw(self):
            c = self.canv
            # Timeline axis
            line_y = 110
            c.setStrokeColor(BRAND_DARK)
            c.setLineWidth(2)
            c.line(30, line_y, PW-20, line_y)

            events = [
                (0,    "Customer\ngoes silent",  DARK_BG),
                (0.22, "Follow-up 1\n2–4 hrs",   FUNNEL_2),
                (0.44, "Follow-up 2\n6 hrs later",FUNNEL_3),
                (0.66, "Follow-up 3\n6–12 hrs",  FUNNEL_4),
                (0.88, "Follow-up 4\n6–18 hrs",  FUNNEL_5),
            ]
            for pct, label, clr in events:
                x = 30 + pct*(PW-50)
                # dot
                c.setFillColor(clr)
                c.circle(x, line_y, 8, fill=1, stroke=0)
                # label above/below alternating
                lines = label.split("\n")
                if events.index((pct, label, clr)) % 2 == 0:
                    y_label = line_y + 20
                    for i, l in enumerate(lines):
                        c.setFillColor(TEXT_DARK)
                        c.setFont("Helvetica-Bold" if i==0 else "Helvetica", 7.5)
                        c.drawCentredString(x, y_label + (len(lines)-1-i)*11, l)
                else:
                    y_label = line_y - 30
                    for i, l in enumerate(lines):
                        c.setFillColor(TEXT_DARK)
                        c.setFont("Helvetica-Bold" if i==0 else "Helvetica", 7.5)
                        c.drawCentredString(x, y_label + (len(lines)-1-i)*11, l)

            # "24h" bracket
            c.setStrokeColor(MID_GREY)
            c.setLineWidth(1)
            c.line(30, line_y-50, PW-20, line_y-50)
            c.setFillColor(MID_GREY)
            c.setFont("Helvetica-Oblique", 8)
            c.drawCentredString(PW/2, line_y-60, "← All follow-ups complete within 24 hours →")

            # "archived" end label
            c.setFillColor(colors.HexColor("#9E9E9E"))
            c.setFont("Helvetica", 7)
            c.drawCentredString(PW-20, line_y-12, "Archived")

    story.append(FollowUpTimeline())
    story.append(Spacer(1, 10))

    follow_up_table = [
        [Paragraph("<b>Lead Temperature</b>", TABLE_HEADER),
         Paragraph("<b>Follow-up 1</b>", TABLE_HEADER),
         Paragraph("<b>Follow-up 2</b>", TABLE_HEADER),
         Paragraph("<b>Follow-up 3</b>", TABLE_HEADER),
         Paragraph("<b>Follow-up 4</b>", TABLE_HEADER)],
        ["VERY HOT",  "2 hrs",  "4 hrs",  "4 hrs",  "6 hrs"],
        ["HOT",       "2 hrs",  "6 hrs",  "6 hrs",  "6 hrs"],
        ["WARM",      "3 hrs",  "6 hrs",  "6 hrs",  "6 hrs"],
        ["COLD",      "4 hrs",  "6 hrs",  "6 hrs",  "6 hrs"],
    ]
    fup = Table(follow_up_table, colWidths=[PW*0.28, PW*0.18, PW*0.18, PW*0.18, PW*0.18])
    fup.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), DARK_BG),
        ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
        ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 9),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[
            colors.HexColor("#FFEBEE"), colors.HexColor("#FFF3E0"),
            colors.HexColor("#E3F2FD"), colors.HexColor("#E8F5E9"),
        ]),
        ("ALIGN",         (1,0), (-1,-1), "CENTER"),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("GRID",          (0,0), (-1,-1), 0.4, MID_GREY),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(fup)
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════════
    # PAGE 9 — MOCK WHATSAPP SCREENSHOTS
    # ════════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 6))
    story.append(SectionHeader("  SYSTEM SCREENSHOTS — WhatsApp Conversation Flows", BRAND_DARK, PW))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "<font color='#795548'><b>Response Timing Note:</b></font>  "
        "Each Plumbot reply is held by a 5-minute human-response timer before delivery. "
        "This deliberate delay makes the conversation feel like a real team member is responding — "
        "building trust and preventing the 'obviously a bot' experience. "
        "The examples below show the message content; in production each bot reply arrives ~5 minutes after the customer's message.",
        BODY))
    story.append(Spacer(1, 8))

    story.append(Paragraph(b("Screenshot 1: New Lead — Bathroom Renovation Enquiry"), H2))
    story.append(Spacer(1, 4))
    story.append(WhatsAppBubble([
        ("bot",  "Hi! Welcome to Homebase Plumbers 👋 I'm Plumbot, your personal plumbing assistant. What can we help you with today?"),
        ("user", "Hi I want to renovate my bathroom"),
        ("bot",  "Great choice — a bathroom reno can transform your whole home! We do full bathroom renovations from US$900. To give you an accurate quote, which area of Harare are you in?"),
        ("user", "Im in Borrowdale"),
        ("bot",  "Perfect, we cover Borrowdale. Do you already have plans/drawings for the bathroom, or would you prefer our plumber does a quick free site visit first to assess the space?"),
        ("user", "No plans yet, I'd like a site visit"),
        ("bot",  "No problem at all! When works best for you — morning or afternoon, and which days this week are you free?"),
        ("user", "Wednesday afternoon"),
        ("bot",  "Wednesday afternoon works. I have 2:00 PM and 4:00 PM available — which suits you better?"),
        ("user", "2pm please"),
        ("bot",  "Confirmed! ✅ Site visit booked for Wednesday 2:00 PM. Our plumber Tendai will be there. You'll receive a reminder the day before. See you Wednesday!"),
    ], PW))
    story.append(Spacer(1, 6))
    story.append(Paragraph(b("Screenshot 2: Pricing Enquiry — Geyser Replacement"), H2))
    story.append(Spacer(1, 4))
    story.append(WhatsAppBubble([
        ("user", "How much does a geyser cost?"),
        ("bot",  "For a geyser replacement we supply and fit from US$350 (standard 100L) up to US$650 (200L + pressure valve). Labour is included. Which size geyser do you currently have, or do you know the size you need?"),
        ("user", "150L I think"),
        ("bot",  "Got it! A 150L geyser supply and fit typically runs US$420–US$480 depending on the brand and your location. Would you like us to come out and confirm the exact spec before we quote? It's free."),
        ("user", "Yes please, I'm in Greendale"),
        ("bot",  "Greendale we cover. When are you available for a free site visit — this week or early next week?"),
    ], PW))
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════════
    # PAGE 10 — NEW FEATURE: GOOGLE REVIEW EMAIL
    # ════════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 6))
    story.append(SectionHeader("  NEW FEATURE: Automated Google Review Request", ACCENT_ORANGE, PW))
    story.append(Spacer(1, 10))

    story.append(Paragraph(
        "Once a plumber marks a job as <b>Completed</b> in the Plumbot dashboard, the system "
        "automatically sends a personalised email to the customer within minutes, "
        "asking them to leave a Google review.", BODY))
    story.append(Spacer(1, 8))

    # Flow
    review_steps = [
        ("Plumber marks job as Completed (dashboard)",          BRAND_DARK),
        ("System triggers post-job email workflow",             FUNNEL_2),
        ("Customer email fetched from appointment record",      FUNNEL_3),
        ("Personalised review email sent via SMTP",             FUNNEL_4),
        ("Customer clicks review link → Google Maps",          FUNNEL_5),
        ("5-star review posted → Google ranking improves",     FUNNEL_7),
    ]
    story.append(FlowChart(review_steps, PW))
    story.append(Spacer(1, 10))

    # Email mockup
    story.append(Paragraph(b("Sample Review Request Email:"), H2))
    story.append(Spacer(1, 4))

    class EmailMockup(Flowable):
        def wrap(self, aw, ah): return PW, 220
        def draw(self):
            c = self.canv
            # email frame
            c.setFillColor(colors.white)
            c.setStrokeColor(MID_GREY)
            c.roundRect(0, 0, PW, 220, 5, fill=1, stroke=1)
            # header band
            c.setFillColor(BRAND_DARK)
            c.rect(0, 192, PW, 28, fill=1, stroke=0)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(12, 205, "Subject: How did your plumbing job go? 🌟")
            c.setFont("Helvetica", 8)
            c.drawRightString(PW-12, 205, "From: noreply@homebaseplumbers.co.zw")
            # body
            body_lines = [
                ("Helvetica-Bold", 12, BRAND_DARK, 170, "Hi Blessing,"),
                ("Helvetica",      9,  TEXT_BODY,  155, "Thank you for choosing Homebase Plumbers! We hope Tendai's visit went smoothly"),
                ("Helvetica",      9,  TEXT_BODY,  142, "and your bathroom renovation is everything you imagined."),
                ("Helvetica",      9,  TEXT_BODY,  125, "If you have 60 seconds, we'd love to hear how it went. Your review helps other"),
                ("Helvetica",      9,  TEXT_BODY,  112, "Harare homeowners find trusted plumbers — and means the world to our small team."),
            ]
            for font, size, clr, y, text in body_lines:
                c.setFont(font, size)
                c.setFillColor(clr)
                c.drawString(12, y, text)
            # button
            c.setFillColor(BRAND_GREEN)
            c.roundRect(PW/2 - 90, 65, 180, 30, 6, fill=1, stroke=0)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 11)
            c.drawCentredString(PW/2, 76, "⭐  Leave a Google Review  ⭐")
            # footer
            c.setFillColor(LIGHT_GREY)
            c.rect(0, 0, PW, 50, fill=1, stroke=0)
            c.setFillColor(MID_GREY)
            c.setFont("Helvetica", 7.5)
            c.drawCentredString(PW/2, 32, "Homebase Plumbers  •  Hatfield, Harare  •  Registered & Insured")
            c.drawCentredString(PW/2, 18, "You received this because you recently had work done by Homebase Plumbers.")
            c.drawCentredString(PW/2, 6,  "Unsubscribe | Privacy Policy")

    story.append(EmailMockup())
    story.append(Spacer(1, 10))

    story.append(Paragraph(b("Why this matters:"), H2))
    review_benefits = [
        "Google reviews directly impact your ranking in Google Maps search results. More reviews = more organic traffic.",
        "93% of consumers check reviews before choosing a local business (BrightLocal 2024).",
        "Asking at the right moment (immediately after a great job) produces 3–5× more responses than asking later.",
        "Fully automated — zero effort required from your team. Every completed job generates a review request.",
        "Compounds over time: 10 new reviews this month means more calls next month, every month.",
    ]
    for item in review_benefits:
        story.append(Paragraph(f"✓  {item}", BULLET))
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════════
    # PAGE 11 — DASHBOARD MOCKUP
    # ════════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 6))
    story.append(SectionHeader("  DASHBOARD — Real-Time Lead Pipeline View", BRAND_DARK, PW))
    story.append(Spacer(1, 10))

    class DashboardMockup(Flowable):
        def wrap(self, aw, ah): return PW, 300
        def draw(self):
            c = self.canv
            # bg
            c.setFillColor(DARK_BG)
            c.roundRect(0, 0, PW, 300, 6, fill=1, stroke=0)
            # top nav
            c.setFillColor(colors.HexColor("#111827"))
            c.rect(0, 270, PW, 30, fill=1, stroke=0)
            c.setFillColor(BRAND_GREEN)
            c.setFont("Helvetica-Bold", 11)
            c.drawString(12, 282, "Plumbot Dashboard")
            c.setFillColor(MID_GREY)
            c.setFont("Helvetica", 8)
            navs = ["Leads", "Appointments", "Quotations", "Reports", "Settings"]
            for i, nav in enumerate(navs):
                c.drawString(120 + i*70, 282, nav)

            # stat cards
            cards = [
                ("12", "Active Leads",       FUNNEL_2),
                ("4",  "This Week's Appts",  FUNNEL_3),
                ("2",  "Very Hot Leads",     colors.HexColor("#EF5350")),
                ("8",  "Reviews This Month", FUNNEL_7),
            ]
            cw = (PW - 30) / 4
            for i, (val, label, clr) in enumerate(cards):
                bx = 10 + i*(cw+4)
                c.setFillColor(clr)
                c.roundRect(bx, 218, cw, 44, 4, fill=1, stroke=0)
                c.setFillColor(colors.white)
                c.setFont("Helvetica-Bold", 20)
                c.drawCentredString(bx+cw/2, 238, val)
                c.setFont("Helvetica", 7.5)
                c.drawCentredString(bx+cw/2, 225, label)

            # lead table header
            c.setFillColor(colors.HexColor("#1F2937"))
            c.rect(10, 196, PW-20, 18, fill=1, stroke=0)
            c.setFillColor(MID_GREY)
            c.setFont("Helvetica-Bold", 7.5)
            cols = ["Customer", "Service", "Area", "Status", "Score", "Last Contact"]
            cws2 = [0.20, 0.20, 0.15, 0.14, 0.09, 0.22]
            x = 14
            for col, cw2 in zip(cols, cws2):
                c.drawString(x, 202, col)
                x += cw2 * (PW-24)

            # table rows
            rows = [
                ("Blessing M.",   "Bathroom Reno",   "Borrowdale",  "VERY HOT", "100", "2 min ago"),
                ("Takudzwa N.",   "Geyser Repair",   "Greendale",   "HOT",      "75",  "1 hr ago"),
                ("Chido F.",      "Kitchen Reno",    "Avondale",    "HOT",      "75",  "3 hrs ago"),
                ("Farai S.",      "New Plumbing",    "Mabelreign",  "WARM",     "50",  "Yesterday"),
                ("Peter K.",      "Repairs",         "Hatfield",    "WARM",     "50",  "Yesterday"),
                ("Rutendo T.",    "Bathroom Reno",   "Highlands",   "COLD",     "25",  "2 days ago"),
            ]
            status_colors = {
                "VERY HOT": colors.HexColor("#B71C1C"),
                "HOT":      colors.HexColor("#EF5350"),
                "WARM":     colors.HexColor("#FF9800"),
                "COLD":     colors.HexColor("#64B5F6"),
            }
            for ri, row in enumerate(rows):
                y = 186 - ri*25
                bg = DARK_BG if ri % 2 == 0 else colors.HexColor("#111827")
                c.setFillColor(bg)
                c.rect(10, y-8, PW-20, 22, fill=1, stroke=0)
                x = 14
                c.setFillColor(colors.white)
                c.setFont("Helvetica", 8)
                for ci, (cell, cw2) in enumerate(zip(row, cws2)):
                    if ci == 3:
                        sc = status_colors.get(cell, MID_GREY)
                        c.setFillColor(sc)
                        c.roundRect(x, y-3, 46, 14, 3, fill=1, stroke=0)
                        c.setFillColor(colors.white)
                        c.setFont("Helvetica-Bold", 6.5)
                        c.drawCentredString(x+23, y+2, cell)
                        c.setFont("Helvetica", 8)
                        c.setFillColor(colors.white)
                    else:
                        c.drawString(x, y+2, cell)
                    x += cw2 * (PW-24)

    story.append(DashboardMockup())
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "The dashboard updates in real time as conversations happen on WhatsApp. "
        "Plumbers and managers can see the full lead pipeline, filter by temperature, "
        "and access the full conversation history for any customer with one click.", BODY))
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════════
    # PAGE 12 — TECHNICAL ARCHITECTURE
    # ════════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 6))
    story.append(SectionHeader("  TECHNICAL ARCHITECTURE", DARK_BG, PW))
    story.append(Spacer(1, 10))

    arch_data = [
        [Paragraph("<b>Component</b>", TABLE_HEADER), Paragraph("<b>Technology</b>", TABLE_HEADER), Paragraph("<b>Purpose</b>", TABLE_HEADER)],
        ["Web Framework",    "Django 5.2",             "Core application logic, admin, API"],
        ["Database",         "PostgreSQL",             "Lead, appointment & conversation data"],
        ["WhatsApp Channel", "Meta WhatsApp Cloud API","Inbound/outbound WhatsApp messaging"],
        ["AI Engine",        "DeepSeek v3-flash",      "Intent classification, NLP, responses"],
        ["Calendar",         "Google Calendar API",    "Appointment booking & conflict detection"],
        ["Email",            "SMTP (Gmail)",           "Customer emails & review requests"],
        ["Cloud Hosting",    "Railway.app",            "Production server & cron jobs"],
        ["File Storage",     "AWS S3 / Local",         "Customer plans, images, documents"],
        ["Cron Jobs",        "Railway Cron",           "Follow-ups, reminders, notifications"],
        ["Security",         "HTTPS + CSRF + WAMID",   "Webhook auth, deduplication, SSL"],
    ]
    at = Table(arch_data, colWidths=[PW*0.22, PW*0.22, PW*0.56])
    at.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), DARK_BG),
        ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
        ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 9),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[LIGHT_GREY, colors.white]),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("GRID",          (0,0), (-1,-1), 0.4, MID_GREY),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
    ]))
    story.append(at)
    story.append(Spacer(1, 14))

    story.append(Paragraph(b("System Architecture Diagram:"), H2))
    story.append(Spacer(1, 6))

    class ArchDiagram(Flowable):
        def wrap(self, aw, ah): return PW, 210
        def draw(self):
            c = self.canv
            boxes = [
                (PW*0.04,  130, "WhatsApp\nCustomer",     BRAND_GREEN),
                (PW*0.27,  130, "Meta\nWebhook",          FUNNEL_2),
                (PW*0.50,  130, "Plumbot\nDjango App",    BRAND_DARK),
                (PW*0.50,  55,  "PostgreSQL\nDatabase",   FUNNEL_3),
                (PW*0.73,  175, "DeepSeek\nAI Engine",    FUNNEL_4),
                (PW*0.73,  130, "Google\nCalendar",       FUNNEL_5),
                (PW*0.73,  85,  "SMTP\nEmail",            FUNNEL_6),
                (PW*0.73,  40,  "AWS S3\nStorage",        colors.HexColor("#607D8B")),
            ]
            bw, bh = 68, 32
            arrows = [
                (0, 1), (1, 2), (2, 3), (2, 4), (2, 5), (2, 6), (2, 7)
            ]

            def draw_box(x, y, label, clr):
                c.setFillColor(clr)
                c.roundRect(x, y, bw, bh, 5, fill=1, stroke=0)
                c.setFillColor(colors.white)
                c.setFont("Helvetica-Bold", 7.5)
                lines = label.split("\n")
                for i, l in enumerate(lines):
                    c.drawCentredString(x+bw/2, y+bh/2 + (0.5-i)*10, l)

            for bx, by, label, clr in boxes:
                draw_box(bx, by, label, clr)

            c.setStrokeColor(BRAND_DARK)
            c.setLineWidth(1.2)
            for (i, j) in arrows:
                bxi, byi = boxes[i][0], boxes[i][1]
                bxj, byj = boxes[j][0], boxes[j][1]
                # start right side of i, end left side of j (if same row) or top/bottom
                if abs(byi - byj) < 20:
                    sx = bxi + bw; sy = byi + bh/2
                    ex = bxj;     ey = byj + bh/2
                    c.line(sx, sy, ex, ey)
                    # arrowhead
                    c.setFillColor(BRAND_DARK)
                    p = c.beginPath()
                    p.moveTo(ex, ey); p.lineTo(ex-7, ey+4); p.lineTo(ex-7, ey-4); p.close()
                    c.drawPath(p, fill=1, stroke=0)
                else:
                    sx = bxi + bw;     sy = byi + bh/2
                    mid_x = bxj - 10
                    ey = byj + bh/2
                    c.line(sx, sy, mid_x, sy)
                    c.line(mid_x, sy, mid_x, ey)
                    c.line(mid_x, ey, bxj, ey)
                    c.setFillColor(BRAND_DARK)
                    p = c.beginPath()
                    p.moveTo(bxj, ey); p.lineTo(bxj-7, ey+4); p.lineTo(bxj-7, ey-4); p.close()
                    c.drawPath(p, fill=1, stroke=0)

    story.append(ArchDiagram())
    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════════
    # PAGE 13 — NEXT STEPS & CLOSE
    # ════════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 6))
    story.append(SectionHeader("  SUMMARY & NEXT STEPS", DARK_BG, PW))
    story.append(Spacer(1, 10))

    story.append(Paragraph(b("What Plumbot delivers for Homebase Plumbers:"), H2))
    summary_items = [
        ("Never miss a lead",         "Human-paced WhatsApp responses (~5 min) 24/7 — weekends and holidays included."),
        ("Qualify leads automatically", "AI extracts service type, area, availability — without manual effort."),
        ("Fill your calendar",         "Automatic booking into Google Calendar with conflict detection."),
        ("Reduce no-shows by 50%+",   "3-stage reminder system (day before, morning, 2 hours before)."),
        ("Recover lost leads",         "Up to 4 automated follow-ups per lead within 24 hours."),
        ("Grow your Google presence",  "Every completed job triggers an automated review request email."),
        ("Speak your customers' language", "Full English and Shona support with natural code-switching."),
        ("See your pipeline clearly",  "Real-time dashboard with lead temperatures, appointments, and stats."),
    ]
    for title, desc in summary_items:
        story.append(Paragraph(f"<b>✓  {title}</b> — {desc}", BULLET))
        story.append(Spacer(1, 3))

    story.append(Spacer(1, 14))
    story.append(HRFlowable(width=PW, color=BRAND_GREEN, thickness=2))
    story.append(Spacer(1, 12))

    story.append(Paragraph(b("Immediate Next Steps:"), H2))
    next_steps = [
        ("Implement Google Review email trigger",     "When job status → completed, fire automated review email via SMTP."),
        ("Configure Google review link",             "Add the Homebase Plumbers Google Maps direct review URL to the email template."),
        ("Test end-to-end funnel",                   "Run a test appointment through to completion and verify the review email fires."),
        ("Dashboard review metric",                  "Add 'Reviews Sent This Month' stat card to the admin dashboard."),
    ]
    ns_data = [[Paragraph("<b>#</b>", TABLE_HEADER), Paragraph("<b>Task</b>", TABLE_HEADER), Paragraph("<b>Detail</b>", TABLE_HEADER)]]
    for i, (task, detail) in enumerate(next_steps, 1):
        ns_data.append([str(i), task, detail])
    ns_t = Table(ns_data, colWidths=[PW*0.05, PW*0.35, PW*0.60])
    ns_t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), BRAND_DARK),
        ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
        ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 9),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[LIGHT_GREY, colors.white]),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("ALIGN",         (0,0), (0,-1), "CENTER"),
        ("GRID",          (0,0), (-1,-1), 0.4, MID_GREY),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
    ]))
    story.append(ns_t)
    story.append(Spacer(1, 20))

    # Closing box
    class CloseBox(Flowable):
        def wrap(self, aw, ah): return PW, 64
        def draw(self):
            c = self.canv
            c.setFillColor(DARK_BG)
            c.roundRect(0, 0, PW, 64, 6, fill=1, stroke=0)
            c.setFillColor(BRAND_GREEN)
            c.setFont("Helvetica-Bold", 12)
            c.drawCentredString(PW/2, 44, "Plumbot — Automated. Intelligent. Always On.")
            c.setFillColor(colors.white)
            c.setFont("Helvetica", 9)
            c.drawCentredString(PW/2, 28, "Built for Homebase Plumbers  •  Harare, Zimbabwe")
            c.setFillColor(MID_GREY)
            c.setFont("Helvetica", 8)
            c.drawCentredString(PW/2, 12, "jones86xi@gmail.com  •  Confidential Document — May 2026")

    story.append(CloseBox())

    # ── Build ──
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    print(f"PDF generated: {out_path}")
    return out_path


if __name__ == "__main__":
    build()
