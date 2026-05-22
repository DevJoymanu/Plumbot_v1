from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Plumbing_CRM.settings")
os.environ.setdefault("DATABASE_URL", "sqlite:///db.sqlite3")
os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")

import django

django.setup()

from django.test import Client
from django.contrib.auth.models import User
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer

from bot.models import Appointment, Quotation


DOCS = ROOT / "docs"
SNAPSHOTS = DOCS / "system_page_snapshots"
SCREENSHOTS = DOCS / "screenshots"
DIAGRAMS = DOCS / "diagrams"
PDF_OUTPUT = DOCS / "Plumbot_System_Screenshots_and_Flow_Diagrams.pdf"
BASE_URL = "http://127.0.0.1:8000/"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


def ensure_dirs():
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    DIAGRAMS.mkdir(parents=True, exist_ok=True)


def staff_user():
    user, _created = User.objects.get_or_create(
        username="plumbot_demo",
        defaults={"email": "demo@example.com", "is_staff": True, "is_superuser": True},
    )
    user.is_staff = True
    user.is_superuser = True
    user.set_password("PlumbotDemo123!")
    user.save()
    return user


def html_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <base href="{BASE_URL}">
  <title>{title}</title>
</head>
{body}
</html>
"""


def save_authenticated_pages() -> list[tuple[str, Path, str]]:
    user = staff_user()
    client = Client(HTTP_HOST="testserver")
    client.force_login(user)

    pages = [
        ("dashboard", "/dashboard/", "Dashboard"),
        ("appointments", "/appointments/", "Appointments"),
        ("priority_leads", "/leads/priority/", "Priority Leads"),
        ("followups", "/followups/", "Follow-Ups"),
        ("jobs", "/jobs/", "Jobs"),
        ("calendar", "/calendar/", "Calendar"),
        ("quotations", "/quotations/", "Quotations"),
        ("quotation_templates", "/templates/", "Quotation Templates"),
    ]

    first_appointment = Appointment.objects.order_by("-created_at").first()
    if first_appointment:
        pages.append(("appointment_detail", f"/appointments/{first_appointment.pk}/", "Appointment Detail"))

    first_quote = Quotation.objects.order_by("-created_at").first()
    if first_quote:
        pages.append(("quotation_detail", f"/quotations/{first_quote.pk}/", "Quotation Detail"))

    saved: list[tuple[str, Path, str]] = []
    for slug, url, title in pages:
        try:
            response = client.get(url)
            if response.status_code not in (200, 302):
                print(f"SKIP {slug}: status {response.status_code}")
                continue
            if response.status_code == 302:
                print(f"SKIP {slug}: redirected to {response['Location']}")
                continue
            content = response.content.decode("utf-8", errors="replace")
            if "<html" in content.lower():
                content = content.replace("<head>", f"<head><base href=\"{BASE_URL}\">", 1)
            else:
                content = html_shell(title, f"<body>{content}</body>")
            path = SNAPSHOTS / f"{slug}.html"
            path.write_text(content, encoding="utf-8")
            saved.append((slug, path, title))
            print(f"SAVED {slug} -> {path}")
        except Exception as exc:
            print(f"SKIP {slug}: {exc}")

    return saved


def screenshot_with_browser(source: str, output: Path, width=1440, height=1000) -> bool:
    browser = CHROME if CHROME.exists() else EDGE
    if not browser.exists():
        print(f"Browser not found: {browser}")
        return False

    cmd = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--hide-scrollbars",
        f"--window-size={width},{height}",
        f"--screenshot={output}",
        source,
    ]
    result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=45)
    ok = output.exists() and output.stat().st_size > 0
    if not ok:
        print(f"SCREENSHOT FAILED {source}: {result.stderr or result.stdout}")
    return ok


def capture_screenshots(saved_pages: list[tuple[str, Path, str]]) -> list[tuple[str, Path]]:
    captures: list[tuple[str, Path]] = []

    login_png = SCREENSHOTS / "01_login.png"
    if screenshot_with_browser(BASE_URL + "login/", login_png):
        captures.append(("Login", login_png))

    for idx, (slug, html_path, title) in enumerate(saved_pages, start=2):
        out = SCREENSHOTS / f"{idx:02d}_{slug}.png"
        if screenshot_with_browser(html_path.resolve().as_uri(), out):
            captures.append((title, out))
            print(f"CAPTURED {title} -> {out}")

    return captures


def write_svg(path: Path, title: str, steps: list[str], color: str = "#0f766e"):
    width = 1400
    height = 220 + len(steps) * 118
    boxes = []
    y = 120
    for i, step in enumerate(steps, start=1):
        boxes.append(
            f"""
  <rect x="110" y="{y}" width="1180" height="72" rx="10" fill="#ffffff" stroke="{color}" stroke-width="2"/>
  <circle cx="148" cy="{y + 36}" r="22" fill="{color}"/>
  <text x="148" y="{y + 43}" text-anchor="middle" font-size="20" font-family="Arial" fill="#ffffff" font-weight="700">{i}</text>
  <text x="190" y="{y + 31}" font-size="22" font-family="Arial" fill="#0f172a" font-weight="700">{step.split('|')[0]}</text>
  <text x="190" y="{y + 57}" font-size="17" font-family="Arial" fill="#475569">{step.split('|')[1] if '|' in step else ''}</text>
"""
        )
        if i < len(steps):
            boxes.append(
                f"""  <path d="M700 {y + 74} L700 {y + 106}" stroke="#94a3b8" stroke-width="3" marker-end="url(#arrow)"/>"""
            )
        y += 118

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <defs>
    <marker id="arrow" markerWidth="12" markerHeight="12" refX="5" refY="3" orient="auto" markerUnits="strokeWidth">
      <path d="M0,0 L0,6 L6,3 z" fill="#94a3b8" />
    </marker>
  </defs>
  <rect width="100%" height="100%" fill="#f8fafc"/>
  <text x="700" y="58" text-anchor="middle" font-family="Arial" font-size="34" fill="#0f172a" font-weight="700">{title}</text>
  <text x="700" y="88" text-anchor="middle" font-family="Arial" font-size="18" fill="#64748b">Flow diagram generated from the Plumbot CRM funnel</text>
  {''.join(boxes)}
</svg>
"""
    path.write_text(svg, encoding="utf-8")


def create_diagrams() -> list[tuple[str, Path]]:
    diagrams = [
        (
            "complete_funnel",
            "Complete Booking-to-Review Funnel",
            [
                "WhatsApp enquiry|Customer asks about service, price, portfolio, or availability.",
                "AI qualification|Plumbot captures service type, area, timeline, budget, plan status, and contact details.",
                "Lead scoring|Hot and very hot leads are prioritized for fast human follow-up.",
                "Booking|Valid slot is selected, appointment is confirmed, and Google Calendar is updated.",
                "Reminders|Customer and plumber receive timely WhatsApp/email reminders.",
                "Site visit|Team completes assessment and records notes.",
                "Quotation|Professional PDF quotation is created and sent via WhatsApp.",
                "Job scheduling|Accepted work is scheduled, assigned, and tracked.",
                "Job completion|Status is marked completed with completion notes.",
                "Google review email|New feature sends a review request while satisfaction is fresh.",
            ],
        ),
        (
            "system_architecture",
            "Plumbot System Architecture",
            [
                "Customer WhatsApp|Incoming message arrives from Meta WhatsApp.",
                "Webhook processor|Django receives the event and de-duplicates messages.",
                "Plumbot brain|AI classification, response generation, language matching, and flow routing.",
                "CRM database|Appointments, leads, conversations, quotes, jobs, reminders, and uploaded files are stored.",
                "Dashboard|Team manages leads, appointments, quotations, follow-ups, jobs, and settings.",
                "External services|WhatsApp Cloud API, Google Calendar, email/SendGrid, file storage, and AI APIs.",
            ],
        ),
        (
            "review_feature",
            "New Google Review Email Flow",
            [
                "Job marked complete|Staff updates the job status after work is delivered.",
                "Eligibility check|System checks customer email and whether a review request was already sent.",
                "Review email sent|Customer receives a thank-you email with direct Google review link.",
                "Tracking updated|Timestamp/status prevents duplicate requests.",
                "Dashboard follow-up|Failed or skipped emails are visible for manual rescue.",
                "Reputation gain|More public reviews improve trust, local search visibility, and future conversion.",
            ],
        ),
    ]

    outputs: list[tuple[str, Path]] = []
    for slug, title, steps in diagrams:
        svg_path = DIAGRAMS / f"{slug}.svg"
        png_path = DIAGRAMS / f"{slug}.png"
        write_svg(svg_path, title, steps)
        if screenshot_with_browser(svg_path.resolve().as_uri(), png_path, width=1400, height=1600):
            outputs.append((title, png_path))
        outputs.append((title + " SVG", svg_path))
    return outputs


def build_pdf(captures: list[tuple[str, Path]], diagram_assets: list[tuple[str, Path]]):
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "Title",
        parent=styles["Title"],
        alignment=TA_CENTER,
        fontSize=22,
        leading=26,
        textColor=colors.HexColor("#0f172a"),
    )
    heading = ParagraphStyle(
        "Heading",
        parent=styles["Heading2"],
        fontSize=14,
        leading=17,
        textColor=colors.HexColor("#0f766e"),
        spaceAfter=8,
    )
    body = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontSize=9.5,
        leading=12,
        textColor=colors.HexColor("#475569"),
    )

    doc = SimpleDocTemplate(
        str(PDF_OUTPUT),
        pagesize=landscape(A4),
        leftMargin=1.2 * cm,
        rightMargin=1.2 * cm,
        topMargin=1.0 * cm,
        bottomMargin=1.0 * cm,
        title="Plumbot Screenshots and Flow Diagrams",
        author="Plumbot CRM",
    )
    story = [
        Paragraph("Plumbot System Screenshots and Flow Diagrams", title),
        Spacer(1, 0.2 * cm),
        Paragraph(
            "Screenshots are captured from the local Django system and diagrams illustrate the operational flow from lead capture through Google review request.",
            body,
        ),
        PageBreak(),
    ]

    png_diagrams = [(name, path) for name, path in diagram_assets if path.suffix.lower() == ".png"]
    for name, path in png_diagrams:
        story.append(Paragraph(name, heading))
        story.append(Image(str(path), width=24.5 * cm, height=15.5 * cm, kind="proportional"))
        story.append(PageBreak())

    for name, path in captures:
        story.append(Paragraph(name, heading))
        story.append(Image(str(path), width=24.5 * cm, height=15.5 * cm, kind="proportional"))
        story.append(PageBreak())

    doc.build(story)


def main():
    ensure_dirs()
    saved_pages = save_authenticated_pages()
    captures = capture_screenshots(saved_pages)
    diagram_assets = create_diagrams()
    build_pdf(captures, diagram_assets)
    print(f"PDF {PDF_OUTPUT}")
    print(f"SCREENSHOTS {SCREENSHOTS}")
    print(f"DIAGRAMS {DIAGRAMS}")


if __name__ == "__main__":
    main()
