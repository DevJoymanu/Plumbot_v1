import base64
import logging
import re
from email.utils import parseaddr

import requests
from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)

DEFAULT_PLUMBER_NOTIFICATION_EMAILS = [
    "jones86xi@gmail.com",
    "homebsconstruction@gmail.com",
]


def get_plumber_notification_emails():
    recipients = getattr(
        settings,
        "PLUMBER_NOTIFICATION_EMAILS",
        DEFAULT_PLUMBER_NOTIFICATION_EMAILS,
    )
    return [email for email in recipients if email]


def send_email_to_recipients(
    recipients, subject, message, *, dry_run=False,
    html_message=None, attachment=None, attachment_name="attachment.pdf",
    from_name=None, message_id=None,
):
    """
    Send email to an explicit list of recipients via the configured SMTP
    backend (Django EMAIL_BACKEND).

    attachment: bytes object (e.g. PDF) to attach, or None.
    attachment_name: filename for the attachment.
    """
    if not recipients:
        logger.warning("send_email_to_recipients: no recipients for '%s'.", subject)
        return False

    if dry_run:
        logger.info(
            "Dry run: would send '%s' to %s", subject, ", ".join(recipients)
        )
        return True

    # Primary transport: SendGrid HTTP API (port 443). Railway blocks all
    # outbound SMTP, so this is the only path that delivers from production.
    # Falls through to Django SMTP below when no API key is configured.
    sendgrid_api_key = getattr(settings, "SENDGRID_API_KEY", "")
    if sendgrid_api_key:
        return _send_via_sendgrid(
            sendgrid_api_key, recipients, subject, message,
            html_message=html_message, attachment=attachment,
            attachment_name=attachment_name, from_name=from_name,
            message_id=message_id,
        )

    try:
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None)
        if from_name:
            _, addr = parseaddr(from_email or "")
            from_email = f"{from_name} <{addr}>" if addr else from_email

        # Reply-To routes replies to a real inbox (and aligns DMARC for
        # Gmail's Primary-routing heuristic). Falls back to the From address
        # so we never send without one.
        _, from_addr_only = parseaddr(from_email or "")
        reply_to_raw = (
            getattr(settings, "EMAIL_REPLY_TO", None)
            or from_addr_only
            or from_email
        )
        reply_to_list = None
        if reply_to_raw:
            _, reply_to_addr = parseaddr(reply_to_raw)
            if reply_to_addr:
                reply_to_list = [reply_to_addr]

        msg = EmailMultiAlternatives(
            subject, message, from_email, recipients,
            reply_to=reply_to_list,
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


def send_plumber_notification_email(subject, message, *, dry_run=False, html_message=None):
    """
    Send a notification email to the configured plumber team inbox(es).
    Delegates to send_email_to_recipients so all deliverability headers
    (Reply-To, X-Entity-Ref-ID) are applied consistently.
    """
    recipients = get_plumber_notification_emails()
    if not recipients:
        logger.warning("No plumber notification email recipients configured.")
        return False

    return send_email_to_recipients(
        recipients, subject, message,
        dry_run=dry_run,
        html_message=html_message,
    )


def _send_via_sendgrid(
    api_key, recipients, subject, message, *, html_message=None,
    attachment=None, attachment_name="attachment.pdf", from_name=None,
    message_id=None,
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

    from_raw = (
        getattr(settings, "SENDGRID_FROM_EMAIL", None)
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
            {"to": [{"email": email} for email in recipients]}
        ],
        "from": sender,
        "subject": subject,
        "content": content,
    }

    # Reply-To: route replies to a real inbox and align DMARC. Mirrors the
    # SMTP path's EMAIL_REPLY_TO → from-address fallback.
    reply_to_raw = (
        getattr(settings, "EMAIL_REPLY_TO", None)
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
