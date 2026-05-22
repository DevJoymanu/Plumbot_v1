from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    KeepTogether,
)


OUTPUT_FILE = "docs/Plumbot_Features_and_Client_Value_Outline.pdf"


def para(text, style):
    return Paragraph(text, style)


def add_table(story, rows, widths, header=True):
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d9e2ec")),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
    ]
    if header:
        style.extend(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ]
        )
    table.setStyle(TableStyle(style))
    story.append(table)
    story.append(Spacer(1, 0.35 * cm))


def draw_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#64748b"))
    canvas.drawString(doc.leftMargin, 1.05 * cm, "Plumbot Feature and Funnel Outline")
    canvas.drawRightString(A4[0] - doc.rightMargin, 1.05 * cm, f"Page {doc.page}")
    canvas.restoreState()


def build_pdf():
    doc = SimpleDocTemplate(
        OUTPUT_FILE,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.35 * cm,
        bottomMargin=1.55 * cm,
        title="Plumbot Feature and Funnel Outline",
        author="Plumbot CRM",
    )

    base = getSampleStyleSheet()
    title = ParagraphStyle(
        "Title",
        parent=base["Title"],
        alignment=TA_CENTER,
        fontSize=22,
        leading=26,
        textColor=colors.HexColor("#0f172a"),
        spaceAfter=8,
    )
    subtitle = ParagraphStyle(
        "Subtitle",
        parent=base["Normal"],
        alignment=TA_CENTER,
        fontSize=10.5,
        leading=14,
        textColor=colors.HexColor("#475569"),
        spaceAfter=18,
    )
    h1 = ParagraphStyle(
        "H1",
        parent=base["Heading1"],
        fontSize=15,
        leading=18,
        textColor=colors.HexColor("#0f766e"),
        spaceBefore=10,
        spaceAfter=7,
    )
    h2 = ParagraphStyle(
        "H2",
        parent=base["Heading2"],
        fontSize=11.5,
        leading=14,
        textColor=colors.HexColor("#0f172a"),
        spaceBefore=8,
        spaceAfter=5,
    )
    body = ParagraphStyle(
        "Body",
        parent=base["BodyText"],
        fontSize=9.2,
        leading=12.5,
        textColor=colors.HexColor("#334155"),
        spaceAfter=6,
    )
    small = ParagraphStyle(
        "Small",
        parent=body,
        fontSize=8.2,
        leading=11,
        textColor=colors.HexColor("#475569"),
    )
    bullet = ParagraphStyle(
        "Bullet",
        parent=body,
        leftIndent=12,
        firstLineIndent=-7,
        bulletIndent=0,
    )
    th = ParagraphStyle("TH", parent=small, textColor=colors.white, fontName="Helvetica-Bold")
    td = ParagraphStyle("TD", parent=small)
    td_bold = ParagraphStyle("TDBold", parent=small, fontName="Helvetica-Bold", textColor=colors.HexColor("#0f172a"))

    story = []
    story.append(para("Plumbot", title))
    story.append(
        para(
            "Feature and functionality outline for Homebase Plumbers, focused on client benefit, revenue gain, and the full booking-to-review funnel.",
            subtitle,
        )
    )

    story.append(para("Executive Summary", h1))
    story.append(
        para(
            "Plumbot is a WhatsApp-first sales, booking, and operations CRM for a plumbing business. It captures leads instantly, qualifies the customer, books site visits, reminds both customer and plumber, supports quotations, manages follow-ups, and keeps the team focused on high-value jobs.",
            body,
        )
    )
    story.append(
        para(
            "The client benefit is simple: fewer missed enquiries, faster response times, better-qualified leads, stronger follow-up discipline, smoother job coordination, and more completed jobs turning into public social proof through Google reviews.",
            body,
        )
    )

    story.append(para("Core Value Proposition", h1))
    rows = [
        [para("Business Problem", th), para("Plumbot Functionality", th), para("Client Benefit / Potential Gain", th)],
        [
            para("Customers message outside office hours or during busy jobs.", td_bold),
            para("Automatic WhatsApp intake and AI-guided responses.", td),
            para("More leads are captured before they drift to competitors. Potential gain: higher enquiry-to-booking conversion.", td),
        ],
        [
            para("Staff spend time asking the same qualification questions.", td_bold),
            para("Plumbot collects service type, area, plan status, budget, timeline, name, email, and preferred availability.", td),
            para("The team receives clearer job context before visiting. Potential gain: less admin time and more productive site visits.", td),
        ],
        [
            para("Hot leads can get buried among casual enquiries.", td_bold),
            para("Lead scoring classifies leads as cold, warm, hot, or very hot.", td),
            para("Sales attention goes to the best opportunities first. Potential gain: more closed jobs from the same lead volume.", td),
        ],
        [
            para("Customers forget appointments or plumbers miss preparation details.", td_bold),
            para("WhatsApp and email reminders, calendar scheduling, and plumber briefings.", td),
            para("Reduced no-shows and better punctuality. Potential gain: fewer wasted trips and smoother daily operations.", td),
        ],
        [
            para("Completed jobs do not always create referrals or online trust.", td_bold),
            para("New feature: job-completion email requesting a Google review.", td),
            para("More public reviews and stronger local credibility. Potential gain: better search trust, more organic leads, and stronger close rates.", td),
        ],
    ]
    add_table(story, rows, [4.4 * cm, 5.1 * cm, 7.4 * cm])

    story.append(PageBreak())
    story.append(para("Feature and Functionality Outline", h1))

    feature_sections = [
        (
            "1. WhatsApp Lead Capture and Conversation Automation",
            [
                ("Inbound WhatsApp webhook", "Receives and processes customer messages in real time.", "Immediate response improves trust and reduces missed enquiries."),
                ("AI-guided response flow", "Handles greetings, service questions, pricing intent, qualification, booking nudges, and objections.", "Customers get helpful answers without waiting for manual staff input."),
                ("English, Shona, and mixed-language support", "Detects customer language and replies naturally in the same style.", "Local customers feel understood, improving engagement and trust."),
                ("Duplicate event handling", "Prevents repeated webhook messages from creating noisy duplicate replies.", "Cleaner conversations and fewer awkward customer experiences."),
                ("Human handoff controls", "The team can pause and resume the chatbot for individual leads.", "Staff can step in for sensitive, high-value, or complex opportunities."),
            ],
        ),
        (
            "2. Qualification and Lead Intelligence",
            [
                ("Four-stage qualification framework", "Moves customers through value, price, qualification, and close stages.", "The sales conversation remains focused on booking a site visit or next action."),
                ("Project data extraction", "Captures project type, area, property type, house stage, plan status, budget, timeline, and description.", "Better job understanding before dispatch improves quote accuracy and professionalism."),
                ("Lead scoring", "Scores and classifies leads as cold, warm, hot, or very hot.", "The team prioritizes customers most likely to buy."),
                ("Priority lead notifications", "Alerts the plumber/team when a very hot lead appears.", "Fast human follow-up can secure revenue before a competitor responds."),
                ("Conversation history and summaries", "Stores customer and bot messages with structured notes.", "Team members can review context quickly without asking the customer to repeat themselves."),
            ],
        ),
        (
            "3. Booking, Availability, and Calendar",
            [
                ("Availability checking", "Checks business hours, closed days, conflicts, minimum notice, and booking window.", "Reduces scheduling mistakes and protects the plumber's calendar."),
                ("Date and time parsing", "Understands customer availability and guides them toward valid booking options.", "Customers can book naturally through chat instead of filling long forms."),
                ("Booking confirmation", "Confirms the site visit and stores scheduled date, end time, duration, and status.", "The customer has clarity and the business gains a reliable appointment record."),
                ("Google Calendar integration", "Adds confirmed appointments to the configured Google Calendar.", "The team can manage jobs from the calendar tools they already use."),
                ("Reschedule detection", "Detects when a customer asks to move the appointment and routes the conversation accordingly.", "Schedule changes are handled without losing the lead."),
            ],
        ),
        (
            "4. Customer Email and Reminder System",
            [
                ("Email capture during booking", "Asks for customer email and sends confirmation when available.", "Creates a second communication channel beyond WhatsApp."),
                ("Booking confirmation email", "Sends appointment details, service, area, and contact links.", "Increases customer confidence and reduces confusion before the visit."),
                ("Customer reminders", "Sends reminder emails and WhatsApp reminders before the appointment.", "Reduces no-shows, late cancellations, and forgotten bookings."),
                ("Delayed-lead emails", "Sends portfolio/pricing or follow-up email when a lead asks to delay.", "Keeps longer-cycle projects alive instead of letting them disappear."),
                ("Inbound email processing", "Customer email replies can be matched back to the appointment and notify the plumber.", "The team stays aware of customer replies across channels."),
            ],
        ),
    ]

    for heading, features in feature_sections:
        story.append(KeepTogether([para(heading, h2)]))
        rows = [[para("Feature", th), para("What It Does", th), para("Client Benefit / Potential Gain", th)]]
        for name, what, gain in features:
            rows.append([para(name, td_bold), para(what, td), para(gain, td)])
        add_table(story, rows, [4.3 * cm, 6.1 * cm, 6.5 * cm])

    story.append(PageBreak())
    more_sections = [
        (
            "5. Follow-Up and Lead Nurturing",
            [
                ("Automatic follow-up stages", "Follows up leads across day 1, day 3, week 1, week 2, and month-style timing.", "Prevents valuable leads from going cold due to silence."),
                ("Manual follow-up tools", "Staff can send individual or bulk follow-ups from the dashboard.", "Gives the team control when a personal push is needed."),
                ("Pause/resume auto-follow-up", "Follow-ups can be disabled or re-enabled per lead.", "Avoids over-messaging customers while keeping active leads organized."),
                ("Inactive/reactivated lead handling", "Leads can be marked inactive or brought back into the pipeline.", "Keeps reporting cleaner and focuses energy on live opportunities."),
                ("Delay signal follow-up", "Detects customers who say they are not ready now and schedules later re-engagement.", "Captures future revenue that would otherwise be forgotten."),
            ],
        ),
        (
            "6. Quotation and Document Workflow",
            [
                ("Create quotations from appointments", "Turns captured appointment data into a quote.", "Less retyping, faster quote turnaround."),
                ("Standalone quotations", "Allows quotes without an existing appointment.", "Supports walk-in, phone, or manually sourced opportunities."),
                ("PDF quotation generation", "Builds professional quotation PDFs.", "Improves perceived professionalism and makes pricing easy to share."),
                ("Send quotations via WhatsApp", "Delivers the quotation directly to the customer.", "Reduces friction between quote creation and customer receipt."),
                ("Quotation templates", "Reusable templates with default items, labour, transport, and optional lines.", "Faster quoting and more consistent pricing across jobs."),
            ],
        ),
        (
            "7. Site Visit, Job Scheduling, and Operations",
            [
                ("Site visit completion", "Marks a site visit as complete and records notes and assessment.", "Turns sales activity into actionable job planning."),
                ("Job appointment creation", "Schedules the actual job after a completed site visit.", "Creates a clear handover from assessment to paid work."),
                ("Plumber assignment", "Assigns a plumber to the job appointment.", "Improves accountability and preparation."),
                ("Job status tracking", "Tracks pending schedule, scheduled, in progress, completed, and cancelled states.", "The team can see where every job sits in the pipeline."),
                ("Job reminders", "Sends job reminders to the customer and plumber before scheduled work.", "Improves attendance, readiness, and customer confidence."),
            ],
        ),
        (
            "8. Media, Portfolio, Plans, and Trust Builders",
            [
                ("Customer plan/file upload", "Receives plans, photos, documents, and videos from customers.", "The plumber can assess scope before visiting or quoting."),
                ("Plan review status", "Tracks pending upload, uploaded, reviewed, and ready to book.", "Complex projects move through a clear process."),
                ("Previous work photos", "Sends completed-work images or portfolio examples on request.", "Builds trust and helps customers visualize the outcome."),
                ("Pricing overview responses", "Handles common service and price questions in English or Shona.", "Customers get enough pricing context to stay engaged."),
                ("Portfolio/pricing PDF by email", "Can attach a portfolio and pricing guide to delayed leads.", "Strengthens credibility and keeps future projects warm."),
            ],
        ),
        (
            "9. Management Dashboard and Reporting",
            [
                ("Main dashboard", "Shows appointments, hot leads, and recent activity.", "Management gets a quick operational snapshot."),
                ("Appointments list and detail pages", "View, update, confirm, complete, cancel, and export appointments.", "Centralizes daily customer management."),
                ("Priority leads dashboard", "Surfaces hot and very hot leads with update controls.", "Helps the team chase the opportunities most likely to pay."),
                ("Follow-up dashboard", "Shows leads awaiting follow-up and manual follow-up actions.", "Keeps pipeline discipline visible."),
                ("Calendar view", "Visual appointment calendar.", "Easier scheduling and workload planning."),
                ("User management and security pages", "Login, profile, user management, security logs, and sessions.", "Protects operational data and controls team access."),
            ],
        ),
    ]

    for heading, features in more_sections:
        story.append(KeepTogether([para(heading, h2)]))
        rows = [[para("Feature", th), para("What It Does", th), para("Client Benefit / Potential Gain", th)]]
        for name, what, gain in features:
            rows.append([para(name, td_bold), para(what, td), para(gain, td)])
        add_table(story, rows, [4.3 * cm, 6.1 * cm, 6.5 * cm])

    story.append(PageBreak())
    story.append(para("Complete Funnel Map", h1))
    story.append(
        para(
            "The funnel below maps the customer journey from first WhatsApp enquiry through booking, site visit, quotation, job completion, and the proposed Google review request.",
            body,
        )
    )

    funnel = [
        ("1. Customer enquiry", "Customer sends WhatsApp message asking about plumbing service, price, availability, area, previous work, or a specific issue.", "Plumbot captures the lead immediately and creates/updates the appointment record.", "No enquiry is wasted."),
        ("2. Greeting and intent handling", "Bot identifies whether the customer is asking a service question, price question, booking request, portfolio request, or needs clarification.", "Customer receives a relevant reply in English, Shona, or mixed language.", "Higher engagement and lower drop-off."),
        ("3. Value and trust building", "Bot explains service value, handles common questions, shares pricing overview or previous work where appropriate.", "Customer gains confidence before committing to a visit.", "More prospects move from curiosity to booking intent."),
        ("4. Qualification", "Bot collects project type, area, timeline, budget, property/house stage, plan status, and description.", "Lead becomes operationally useful before a staff member gets involved.", "Better site visit preparation and quote accuracy."),
        ("5. Lead scoring and priority alert", "System recalculates score and classifies lead as cold, warm, hot, or very hot.", "Very hot leads can trigger admin/plumber notification and bot pause for human takeover.", "Faster response to the most valuable opportunities."),
        ("6. Availability selection", "Bot offers or parses preferred appointment date/time while checking scheduling rules.", "Customer chooses a workable slot.", "Cleaner calendar, fewer back-and-forth messages."),
        ("7. Booking confirmation", "Appointment is confirmed, Google Calendar event is created, WhatsApp confirmation is sent, and email is requested if missing.", "Customer and team both have confirmed details.", "Reduced no-shows and stronger professionalism."),
        ("8. Pre-visit reminders", "Customer receives reminder messages/emails; plumber receives schedule briefings and upcoming job alerts.", "Both sides are prepared before the visit.", "Fewer wasted trips and better arrival readiness."),
        ("9. Site visit completion", "Team marks the site visit completed and records notes, assessment, and next steps.", "Sales context becomes a structured job record.", "Less information loss between visit and quote."),
        ("10. Quotation", "Team creates a quotation from appointment data or a reusable template, generates a PDF, and sends it via WhatsApp.", "Customer receives a clear, professional price breakdown.", "Faster quote turnaround and stronger close rate."),
        ("11. Job scheduling", "Once accepted, the actual job is scheduled, assigned, and tracked separately from the original site visit.", "Customer gets a clear work date and team knows the scope/materials.", "Better operational planning and accountability."),
        ("12. Job reminders and execution", "Customer and plumber receive job reminders before work starts; job status moves through scheduled, in progress, and completed.", "Work happens with fewer surprises.", "More reliable job delivery and better customer satisfaction."),
        ("13. Job completion", "Team marks the job as completed and stores completion notes.", "The system knows the customer is now ready for aftercare.", "Creates a trigger point for retention and reputation building."),
        ("14. New feature: Google review email", "Immediately after completion, customer receives a polite email thanking them and asking for a Google review, with a direct review link.", "Happy customers are guided to leave public proof while the positive experience is fresh.", "More Google reviews, stronger local SEO, better trust, and more future bookings."),
        ("15. Post-completion reporting", "Dashboard keeps completed appointment/job history, notes, quotes, and customer details.", "Management can see what converted and where revenue came from.", "Better decisions on sales, follow-up, staffing, and marketing."),
    ]
    rows = [[para("Stage", th), para("Customer / Team Action", th), para("System Action", th), para("Business Gain", th)]]
    for stage, action, system, gain in funnel:
        rows.append([para(stage, td_bold), para(action, td), para(system, td), para(gain, td)])
    add_table(story, rows, [3.3 * cm, 4.8 * cm, 4.8 * cm, 4.0 * cm])

    story.append(PageBreak())
    story.append(para("New Google Review Feature Specification", h1))
    story.append(
        para(
            "The requested new feature should trigger when a job is marked completed. If the customer has an email address, Plumbot sends a short thank-you email with a direct Google review link. If no email is available, the dashboard can flag the customer for manual WhatsApp follow-up or prompt staff to add an email before completion.",
            body,
        )
    )
    review_rows = [
        [para("Component", th), para("Recommended Behaviour", th), para("Client Benefit / Potential Gain", th)],
        [para("Trigger", td_bold), para("When job_status or status changes to completed, or when job_completed_at is set.", td), para("Review request happens at the best moment: right after successful delivery.", td)],
        [para("Channel", td_bold), para("Email first, because the user specifically requested email. Optional future WhatsApp fallback can be added.", td), para("Keeps aftercare professional and avoids crowding the WhatsApp sales conversation.", td)],
        [para("Message", td_bold), para("Thank customer, mention the completed service, ask for a quick Google review, include direct review link, and keep the tone personal.", td), para("Higher review completion because the request is easy and timely.", td)],
        [para("Tracking", td_bold), para("Add fields such as google_review_email_sent_at and google_review_requested to prevent duplicate requests.", td), para("Protects customer experience and keeps reporting reliable.", td)],
        [para("Dashboard signal", td_bold), para("Show whether the review email was sent, skipped, or failed.", td), para("Staff can manually rescue missed review opportunities.", td)],
        [para("Business impact", td_bold), para("More Google reviews improve local trust, search visibility, and conversion from future leads.", td), para("Potential gain: stronger reputation flywheel and lower dependency on paid lead sources.", td)],
    ]
    add_table(story, review_rows, [4.1 * cm, 7.0 * cm, 5.8 * cm])

    story.append(para("Client-Facing Benefits to Emphasize", h1))
    benefits = [
        "More leads captured instantly through WhatsApp, including after-hours enquiries.",
        "Less manual admin because Plumbot qualifies customers before staff intervene.",
        "Better lead prioritization through hot and very hot scoring.",
        "Fewer missed appointments through reminders and calendar scheduling.",
        "Faster, more professional quotations through templates and PDFs.",
        "Better operational handover from site visit to scheduled job.",
        "More repeatable reputation growth through automated Google review requests after completed work.",
        "A clearer management view of leads, appointments, quotes, follow-ups, and jobs.",
    ]
    for item in benefits:
        story.append(Paragraph(item, bullet, bulletText="-"))

    story.append(Spacer(1, 0.25 * cm))
    story.append(para("Suggested Implementation Note", h1))
    story.append(
        para(
            "To implement the Google review feature cleanly, add a customer email utility such as send_google_review_request_email(appointment), add duplicate-prevention fields on the Appointment model, and call the email sender when the job is marked completed in the job status update flow. The Google review URL should be stored in settings or environment variables so it can be changed without code edits.",
            body,
        )
    )

    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)


if __name__ == "__main__":
    build_pdf()
    print(OUTPUT_FILE)
