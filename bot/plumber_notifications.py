import base64
import logging
import re
from email.utils import parseaddr

import requests
from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)

# The platform operator's inbox. Constant for every tenant — it is added to
# every notification regardless of what the tenant chose for itself, so the
# operator sees the same alerts the business does.
PLATFORM_NOTIFICATION_EMAIL = "jones86xi@gmail.com"

DEFAULT_PLUMBER_NOTIFICATION_EMAILS = [
    PLATFORM_NOTIFICATION_EMAIL,
    "homebsconstruction@gmail.com",
]

# Temporarily muted recipients — these are filtered out of every plumber
# notification regardless of which list they came from. Add an address here
# (case-insensitive) to mute it. Currently none are muted.
MUTED_NOTIFICATION_EMAILS: set[str] = set()


def tenant_notification_email(tenant=None):
    """The address THIS tenant chose for its own alerts (Profile page →
    `TenantProfile.email_sender`), or '' if it never set one."""
    if tenant is None:
        return ""
    try:
        profile = getattr(tenant, "profile", None)
        return ((getattr(profile, "email_sender", "") or "").strip()) if profile else ""
    except Exception:
        # No profile row yet (OneToOne raises) — treat as "not chosen".
        return ""


def _tenant_profile(tenant):
    if tenant is None:
        return None
    try:
        return getattr(tenant, "profile", None)
    except Exception:
        # No profile row yet (OneToOne raises).
        return None


def _format_sender(address, display_name=None):
    """'Name <addr>' when we have a name, else the bare address."""
    if not address:
        return ""
    return f"{display_name} <{address}>" if display_name else address


def tenant_platform_from_email(tenant=None):
    """The PLATFORM sending identity for this tenant:
    <tenant-slug>@notifications.homexmedia.com.

    Used for internal notifications — the mail that goes to the operator and to
    the tenant's own inbox. The platform owns this domain, so these always
    deliver regardless of what (if anything) the tenant has set up on their own
    domain. Platform-level mail with no tenant keeps DEFAULT_FROM_EMAIL.

    One shared domain with a per-tenant local part, NOT a per-tenant subdomain:
    each distinct domain needs its own SPF/DKIM records and consumes one of the
    provider's authenticated-domain slots, so a subdomain-per-tenant shape would
    add DNS work and a plan limit to every onboarding. See settings.
    """
    slug = (getattr(tenant, "slug", "") or "").strip().lower()
    if not slug:
        return getattr(settings, "DEFAULT_FROM_EMAIL", "") or ""
    domain = getattr(settings, "PLATFORM_EMAIL_DOMAIN", "notifications.homexmedia.com")
    profile = _tenant_profile(tenant)
    display = (getattr(profile, "email_from_name", "") or "").strip() or (
        getattr(tenant, "name", "") or ""
    ).strip() or None
    return _format_sender(f"{slug}@{domain}", display)


def tenant_customer_from_email(tenant=None):
    """The sending identity for mail to the TENANT'S CLIENTS: the tenant's own
    domain address (`TenantProfile.customer_from_email`), so their customers
    only ever see the tenant's brand.

    Falls back to the tenant's platform subdomain sender when they have not
    configured one — always deliverable, still tenant-branded by display name —
    and to DEFAULT_FROM_EMAIL for platform mail that belongs to no tenant.
    """
    profile = _tenant_profile(tenant)
    own = (getattr(profile, "customer_from_email", "") or "").strip()
    if own:
        display = (getattr(profile, "email_from_name", "") or "").strip() or (
            getattr(tenant, "name", "") or ""
        ).strip() or None
        return _format_sender(own, display)
    return tenant_platform_from_email(tenant)


def get_plumber_notification_emails(tenant=None):
    """Who this tenant's internal alerts go to.

    `PLATFORM_NOTIFICATION_EMAIL` is on every list, always. Beyond that the
    tenant's own chosen address wins (set on the Profile page), so a new tenant's
    lead alerts land in THEIR inbox instead of Homebase's. Only when no address
    has been chosen do we fall back to the legacy configured list — and that
    fallback is limited to platform-level sends (`tenant=None`) and the homebase
    seed, whose second address is genuinely theirs; any other tenant with no
    chosen address gets the operator inbox alone rather than a stranger's.
    """
    chosen = tenant_notification_email(tenant)
    legacy = getattr(
        settings,
        "PLUMBER_NOTIFICATION_EMAILS",
        DEFAULT_PLUMBER_NOTIFICATION_EMAILS,
    )
    if chosen:
        recipients = [PLATFORM_NOTIFICATION_EMAIL, chosen]
    elif tenant is None or (getattr(tenant, "slug", "") or "").lower() == "homebase":
        recipients = [PLATFORM_NOTIFICATION_EMAIL, *legacy]
    else:
        recipients = [PLATFORM_NOTIFICATION_EMAIL]

    muted = {e.lower() for e in MUTED_NOTIFICATION_EMAILS}
    seen = set()
    deduped = []
    for email in recipients:
        addr = (email or "").strip()
        key = addr.lower()
        if not addr or key in seen or key in muted:
            continue
        seen.add(key)
        deduped.append(addr)
    return deduped


def split_notification_recipients(tenant=None):
    """(visible, hidden) for this tenant's internal alerts.

    The tenant's own addresses go in To; `PLATFORM_NOTIFICATION_EMAIL` goes in
    Bcc, so the operator still receives every alert but tenants never see the
    platform address -- it is on the To line of an email they read, which no
    dashboard change could hide.

    If the tenant chose no address of their own there is nobody else to put in
    To, so the platform address stays visible rather than sending a message with
    no recipient at all.
    """
    recipients = get_plumber_notification_emails(tenant)
    platform = PLATFORM_NOTIFICATION_EMAIL.lower()
    visible = [a for a in recipients if a.lower() != platform]
    hidden = [a for a in recipients if a.lower() == platform]
    if not visible:
        return hidden, []
    return visible, hidden


def send_email_to_recipients(
    recipients, subject, message, *, dry_run=False,
    html_message=None, attachment=None, attachment_name="attachment.pdf",
    from_name=None, message_id=None, tenant=None, from_email=None, bcc=None,
):
    """
    Send email to an explicit list of recipients via the configured SMTP
    backend (Django EMAIL_BACKEND).

    attachment: bytes object (e.g. PDF) to attach, or None.
    attachment_name: filename for the attachment.
    bcc: recipients that must receive the mail without appearing on it -- the
        platform operator on tenant alerts. Never put an address here that the
        visible recipients are meant to see.
    from_email: explicit From identity ('Name <addr>' or a bare address).
        Internal notifications pass the tenant's platform subdomain sender; every
        other caller leaves it None and gets the CUSTOMER identity — the tenant's
        own domain address — resolved here, so the tenant's clients never see a
        platform address.
    tenant: the tenant this mail belongs to. Every tenant-scoped send passes it,
        and the tenant's outbound-email switch is honoured here — this is the one
        choke point all mail goes through, so the gate cannot be sidestepped by a
        new caller. Omit it only for PLATFORM mail (dashboard password resets),
        which belongs to no tenant and is always allowed.
    """
    bcc = [a for a in (bcc or []) if a]
    if not recipients:
        logger.warning("send_email_to_recipients: no recipients for '%s'.", subject)
        return False

    from .platform_flags import email_sending_enabled
    if not email_sending_enabled(tenant):
        logger.info(
            "Email OFF for tenant=%s — skipped '%s' to %s",
            getattr(tenant, 'slug', None), subject, ", ".join(recipients),
        )
        return False

    if dry_run:
        logger.info(
            "Dry run: would send '%s' to %s", subject, ", ".join(recipients)
        )
        return True

    # Sender identity. Explicit `from_email` wins (internal alerts pass the
    # platform subdomain); otherwise this is customer-facing mail and goes out
    # from the tenant's own domain.
    from_email = from_email or tenant_customer_from_email(tenant)
    _, from_addr_only = parseaddr(from_email or "")
    # Reply-To must stay on the same identity as From — a tenant-scoped send
    # replying to the platform's global inbox would route a customer's reply to
    # the wrong business. Only unscoped platform mail uses EMAIL_REPLY_TO.
    if tenant is not None and from_addr_only:
        reply_to = from_addr_only
    else:
        reply_to = getattr(settings, "EMAIL_REPLY_TO", None) or from_addr_only

    # Primary transport: Brevo HTTP API (port 443). Railway blocks all outbound
    # SMTP, so an HTTPS API is the only path that delivers from production.
    # Precedence: Brevo → SendGrid (legacy fallback) → Django SMTP.
    brevo_api_key = getattr(settings, "BREVO_API_KEY", "")
    if brevo_api_key:
        return _send_via_brevo(
            brevo_api_key, recipients, subject, message,
            html_message=html_message, attachment=attachment,
            attachment_name=attachment_name, from_name=from_name,
            message_id=message_id, from_email=from_email, reply_to=reply_to,
            bcc=bcc,
        )

    sendgrid_api_key = getattr(settings, "SENDGRID_API_KEY", "")
    if sendgrid_api_key:
        return _send_via_sendgrid(
            sendgrid_api_key, recipients, subject, message,
            html_message=html_message, attachment=attachment,
            attachment_name=attachment_name, from_name=from_name,
            message_id=message_id, from_email=from_email, reply_to=reply_to,
            bcc=bcc,
        )

    try:
        if from_name and from_addr_only:
            from_email = f"{from_name} <{from_addr_only}>"

        # Reply-To routes replies to a real inbox (and aligns DMARC for
        # Gmail's Primary-routing heuristic). Falls back to the From address
        # so we never send without one.
        reply_to_list = None
        _, reply_to_addr = parseaddr(reply_to or from_email or "")
        if reply_to_addr:
            reply_to_list = [reply_to_addr]

        msg = EmailMultiAlternatives(
            subject, message, from_email, recipients,
            reply_to=reply_to_list, bcc=bcc or None,
        )
        if message_id:
            msg.extra_headers["Message-ID"] = message_id
            # X-Entity-Ref-ID gives Gmail a stable per-thread identity tied
            # to the appointment PK — reads as transactional, not bulk.
            m = re.search(r"<apt-(\d+)\.", message_id)
            if m:
                msg.extra_headers["X-Entity-Ref-ID"] = f"apt-{m.group(1)}"
        if html_message:
            msg.attach_alternative(html_message, "text/html")
        if attachment:
            msg.attach(attachment_name, attachment, "application/pdf")
        msg.send(fail_silently=False)
        return True
    except Exception:
        logger.exception(
            "Failed to send email '%s' to %s", subject, ", ".join(recipients)
        )
        return False


def send_plumber_notification_email(subject, message, *, dry_run=False,
                                    html_message=None, tenant=None):
    """
    Send a notification email to the configured plumber team inbox(es).
    Delegates to send_email_to_recipients so all deliverability headers
    (Reply-To, X-Entity-Ref-ID) are applied consistently — and so the tenant's
    outbound-email switch is honoured. Pass the tenant the alert is about.
    """
    recipients, hidden = split_notification_recipients(tenant)
    if not recipients:
        logger.warning("No plumber notification email recipients configured.")
        return False

    return send_email_to_recipients(
        recipients, subject, message,
        bcc=hidden,
        dry_run=dry_run,
        html_message=html_message,
        tenant=tenant,
        # Internal alerts (operator + the tenant's own inbox) always send from
        # the platform subdomain, never the tenant's customer-facing domain.
        from_email=tenant_platform_from_email(tenant),
    )


def send_plumber_followup_alert(appointment, *, reason, follow_up_date_str=None,
                                released_slot_str=None, dry_run=False):
    """
    Email the plumber to personally follow up a lead the bot could not fully
    close on its own. Two cases:
      • 'no_email_followup' — lead wants a follow-up later but gave no email, so
        the automated email sequence can't reach them.
      • 'date_no_time'      — lead booked a DATE but no time; needs a call to
        pin the time before the slot can be confirmed.
      • 'visit_deferred'    — a CONFIRMED visit the lead has since deferred. The
        slot is released; the plumber was told it was on, so they have to be
        told it is off before they drive out to it.

    Plain and actionable, with a click-to-WhatsApp link so the plumber can reach
    the lead in one tap. Reuses the same delivery path as every other plumber
    notification.
    """
    from bot.customer_emails import _clean_phone, _fmt_date, _service

    name    = getattr(appointment, "customer_name", "") or "Unknown"
    phone   = _clean_phone(getattr(appointment, "phone_number", ""))
    service = _service(appointment)
    area    = getattr(appointment, "customer_area", "") or "not given"
    desc    = (getattr(appointment, "project_description", "") or "").strip() or "not given"

    reason_detail = {
        "no_email_followup": (
            "This lead asked to be followed up later but did NOT share an email, "
            "so the automated email sequence can't reach them. Please follow up "
            "on WhatsApp."
        ),
        "date_no_time": (
            "This lead gave an appointment DATE but no time. Please call or "
            "WhatsApp to confirm a time so the booking can be locked in."
        ),
        "visit_deferred": (
            "This lead has DEFERRED the visit that was booked, so the slot has "
            "been released - please do NOT travel. The bot will check back in "
            "with them on the date below."
        ),
    }.get(reason, str(reason))

    when_line  = f"\nAgreed follow-up date: {follow_up_date_str}" if follow_up_date_str else ""
    sched_line = ""
    if reason == "date_no_time" and getattr(appointment, "scheduled_datetime", None):
        sched_line = f"\nBooked date (no time yet): {_fmt_date(appointment)}"
    if released_slot_str:
        sched_line = f"{sched_line}\nCancelled slot (do not attend): {released_slot_str}"

    wa_link = f"https://wa.me/{phone}" if phone else ""
    # A released slot is not a follow-up chore — it changes the plumber's day,
    # so it must be readable as such in the inbox list.
    subject = (f"[Visit cancelled] {name} - {service}"
               if reason == "visit_deferred"
               else f"[Lead follow-up] {name} - {service}")
    message = (
        f"{reason_detail}\n\n"
        f"Customer: {name}\n"
        f"WhatsApp: {phone}  {wa_link}\n"
        f"Service: {service}\n"
        f"Area: {area}\n"
        f"Project: {desc}"
        f"{when_line}{sched_line}\n"
    )
    return send_plumber_notification_email(
        subject, message, dry_run=dry_run,
        tenant=getattr(appointment, 'tenant', None))


def _send_via_brevo(
    api_key, recipients, subject, message, *, html_message=None,
    attachment=None, attachment_name="attachment.pdf", from_name=None,
    message_id=None, from_email=None, reply_to=None, bcc=None,
):
    """
    Send via the Brevo (ex-Sendinblue) transactional API over HTTPS (port 443).

    Carries the same deliverability signals as the SendGrid/SMTP paths — Reply-To
    (DMARC alignment / real reply inbox) and X-Entity-Ref-ID (stable
    per-appointment identity so Gmail reads it as transactional) — so routing to
    Primary is unchanged regardless of transport.

    Brevo has no global tracking toggle in the send payload like SendGrid; its
    free plan does not rewrite links for click-tracking, so tel:/wa.me links stay
    clean without extra settings.
    """
    # The caller's resolved identity wins — it carries the per-tenant sending
    # domain. The configured globals are only the platform-mail fallback.
    from_raw = (
        from_email
        or getattr(settings, "BREVO_FROM_EMAIL", None)
        or getattr(settings, "DEFAULT_FROM_EMAIL", None)
        or ""
    )
    parsed_name, parsed_email = parseaddr(from_raw)
    sender = {"email": parsed_email or from_raw}
    display_name = from_name or parsed_name or None
    if display_name:
        sender["name"] = display_name

    payload = {
        "sender": sender,
        "to": [{"email": email} for email in recipients],
        "subject": subject,
    }
    if bcc:
        payload["bcc"] = [{"email": email} for email in bcc]
    if html_message:
        payload["htmlContent"] = html_message
    if message:
        payload["textContent"] = message
    if not html_message and not message:
        payload["textContent"] = "(no content)"

    # Reply-To: route replies to a real inbox and align DMARC. Mirrors the
    # SendGrid/SMTP EMAIL_REPLY_TO → from-address fallback.
    reply_to_raw = (
        reply_to
        or getattr(settings, "EMAIL_REPLY_TO", None)
        or parsed_email
        or from_raw
    )
    if reply_to_raw:
        _, reply_to_addr = parseaddr(reply_to_raw)
        if reply_to_addr:
            payload["replyTo"] = {"email": reply_to_addr}

    headers = {}
    if message_id:
        headers["Message-ID"] = message_id
        m = re.search(r"<apt-(\d+)\.", message_id)
        if m:
            headers["X-Entity-Ref-ID"] = f"apt-{m.group(1)}"
    if headers:
        payload["headers"] = headers

    if attachment:
        payload["attachment"] = [{
            "content": base64.b64encode(attachment).decode(),
            "name":    attachment_name,
        }]

    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=getattr(settings, "EMAIL_TIMEOUT", 20),
        )
        if 200 <= response.status_code < 300:
            return True
        logger.error(
            "Brevo email send failed for '%s' to %s: %s %s",
            subject, ", ".join(recipients), response.status_code, response.text,
        )
        return False
    except Exception:
        logger.exception(
            "Failed to send Brevo email '%s' to %s", subject, ", ".join(recipients)
        )
        return False


def _send_via_sendgrid(
    api_key, recipients, subject, message, *, html_message=None,
    attachment=None, attachment_name="attachment.pdf", from_name=None,
    message_id=None, from_email=None, reply_to=None, bcc=None,
):
    """
    Send via the SendGrid v3 HTTP API over HTTPS (port 443).

    Carries the same deliverability signals as the SMTP path — Reply-To (DMARC
    alignment / real reply inbox) and X-Entity-Ref-ID (stable per-appointment
    identity so Gmail reads it as transactional) — so routing to Primary is
    unchanged regardless of transport.
    """
    content = []
    if message:
        content.append({"type": "text/plain", "value": message})
    if html_message:
        content.append({"type": "text/html", "value": html_message})
    if not content:
        content = [{"type": "text/plain", "value": "(no content)"}]

    # The caller's resolved identity wins — it carries the per-tenant sending
    # domain. The configured globals are only the platform-mail fallback.
    from_raw = (
        from_email
        or getattr(settings, "SENDGRID_FROM_EMAIL", None)
        or getattr(settings, "DEFAULT_FROM_EMAIL", None)
        or ""
    )
    parsed_name, parsed_email = parseaddr(from_raw)
    sender = {"email": parsed_email or from_raw}
    display_name = from_name or parsed_name or None
    if display_name:
        sender["name"] = display_name

    payload = {
        "personalizations": [
            dict(
                {"to": [{"email": email} for email in recipients]},
                **({"bcc": [{"email": email} for email in bcc]} if bcc else {}),
            )
        ],
        "from": sender,
        "subject": subject,
        "content": content,
    }

    # Reply-To: route replies to a real inbox and align DMARC. Mirrors the
    # SMTP path's EMAIL_REPLY_TO → from-address fallback.
    reply_to_raw = (
        reply_to
        or getattr(settings, "EMAIL_REPLY_TO", None)
        or parsed_email
        or from_raw
    )
    if reply_to_raw:
        _, reply_to_addr = parseaddr(reply_to_raw)
        if reply_to_addr:
            payload["reply_to"] = {"email": reply_to_addr}

    headers = {}
    if message_id:
        headers["Message-ID"] = message_id
        m = re.search(r"<apt-(\d+)\.", message_id)
        if m:
            headers["X-Entity-Ref-ID"] = f"apt-{m.group(1)}"
    if headers:
        payload["headers"] = headers

    if attachment:
        payload["attachments"] = [{
            "content":  base64.b64encode(attachment).decode(),
            "type":     "application/pdf",
            "filename": attachment_name,
        }]

    # Disable all SendGrid tracking. Click-tracking rewrites every link through
    # a sendgrid.net tracking domain and open-tracking injects a 1px pixel —
    # both are strong Gmail "bulk/promotional" signals that push transactional
    # mail to the Promotions tab. Turning them off keeps tel:/wa.me links clean
    # and lets these read as personal 1:1 email (Primary/Updates).
    payload["tracking_settings"] = {
        "click_tracking":        {"enable": False, "enable_text": False},
        "open_tracking":         {"enable": False},
        "subscription_tracking": {"enable": False},
        "ganalytics":            {"enable": False},
    }

    try:
        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=getattr(settings, "EMAIL_TIMEOUT", 20),
        )
        if 200 <= response.status_code < 300:
            return True
        logger.error(
            "SendGrid email send failed for '%s' to %s: %s %s",
            subject, ", ".join(recipients), response.status_code, response.text,
        )
        return False
    except Exception:
        logger.exception(
            "Failed to send SendGrid email '%s' to %s", subject, ", ".join(recipients)
        )
        return False


def send_site_visit_form_email(report, *, dry_run=False):
    """Email the plumber the tokenized debrief form, 35 minutes after the visit.

    This is the FALLBACK entry point — the in-app banner on the appointment
    screen is the primary one, and it opens the very same URL, so whichever the
    plumber uses first closes the other out. Callers must check
    ``report.submitted_at`` before sending; this function does not re-check the
    gate, it just delivers the link.
    """
    from bot.customer_emails import _clean_phone, _fmt_date, _service
    from bot.post_visit import form_url

    apt = report.appointment
    name = (getattr(apt, 'customer_name', '') or '').strip() or 'Unknown'
    phone = _clean_phone(getattr(apt, 'phone_number', ''))
    link = form_url(report)

    subject = f"[Site visit] How did {name} go?"
    message = (
        f"The site visit for {name} is done. Two minutes to log the outcome and "
        f"we take it from there:\n\n"
        f"{link}\n\n"
        f"The form asks how the visit went, when the customer expects the job "
        f"done, and any notes for the quote. It carries straight into the quote "
        f"screen, and the customer follow-ups are scheduled off your answers.\n\n"
        f"Customer: {name}\n"
        f"WhatsApp: {phone}  " + (f"https://wa.me/{phone}" if phone else "") + "\n"
        f"Service: {_service(apt)}\n"
        f"Area: {getattr(apt, 'customer_area', '') or 'not given'}\n"
        f"Visit: {_fmt_date(apt)}\n\n"
        f"This link works once. If you already logged it in the app, ignore this.\n"
    )
    html = (
        f'<p>The site visit for <strong>{name}</strong> is done. Two minutes to '
        f'log the outcome and we take it from there.</p>'
        f'<p><a href="{link}" style="display:inline-block;background:#0f766e;'
        f'color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:6px;'
        f'font-size:16px;font-weight:bold;">Log the site visit</a></p>'
        f'<p style="color:#555;font-size:14px;">It asks how the visit went, when '
        f'the customer expects the job done, and any notes for the quote. Your '
        f'answers schedule the customer follow-ups.</p>'
        f'<p style="font-size:14px;color:#444;">'
        f'Customer: {name}<br>'
        f'WhatsApp: {phone}<br>'
        f'Service: {_service(apt)}<br>'
        f'Area: {getattr(apt, "customer_area", "") or "not given"}<br>'
        f'Visit: {_fmt_date(apt)}</p>'
        f'<p style="font-size:13px;color:#888;">This link works once. If you have '
        f'already logged it in the app, ignore this email.</p>'
    )
    return send_plumber_notification_email(
        subject, message, dry_run=dry_run, html_message=html,
        tenant=getattr(apt, 'tenant', None))


def send_post_visit_handback_email(appointment, *, reason, dry_run=False):
    """Hand a post-visit lead back to the plumber for a manual close.

    Two reasons, both ends of the automated road:
      • 'gone_cold'  — three asks went out and nothing came back.
      • 'no_email'   — there is no address to follow up on, and the debrief form
                       (the only place to add one) was never submitted.
    """
    from bot.customer_emails import _clean_phone, _service

    name = (getattr(appointment, 'customer_name', '') or '').strip() or 'Unknown'
    phone = _clean_phone(getattr(appointment, 'phone_number', ''))
    wa_link = f"https://wa.me/{phone}" if phone else ''
    detail = {
        'gone_cold': (
            "We sent this lead three follow-ups after the site visit and got "
            "nothing back, so the automated sequence has stopped and the lead is "
            "marked cold. It is yours now — a call will do more than another "
            "email at this point."
        ),
        'no_email': (
            "This site visit is over but we have no email address for the "
            "customer and the visit form was never filled in, so there is no way "
            "to follow up automatically. Please pick this one up directly."
        ),
    }.get(reason, str(reason))

    subject = (f"[Lead cold] {name} - {_service(appointment)}"
               if reason == 'gone_cold'
               else f"[Needs you] {name} - {_service(appointment)}")
    message = (
        f"{detail}\n\n"
        f"Customer: {name}\n"
        f"WhatsApp: {phone}  {wa_link}\n"
        f"Service: {_service(appointment)}\n"
        f"Area: {getattr(appointment, 'customer_area', '') or 'not given'}\n"
        f"Lead: {settings.SITE_URL}/appointments/{appointment.pk}/\n"
    )
    return send_plumber_notification_email(
        subject, message, dry_run=dry_run,
        tenant=getattr(appointment, 'tenant', None))
