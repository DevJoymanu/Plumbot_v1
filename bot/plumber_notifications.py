import logging

import requests
from django.conf import settings
from django.core.mail import send_mail

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


def send_email_to_recipients(recipients, subject, message, *, dry_run=False, html_message=None):
    """Send email to an explicit list of recipients (used for per-plumber routing)."""
    if not recipients:
        logger.warning("send_email_to_recipients: no recipients for '%s'.", subject)
        return False

    if dry_run:
        logger.info(
            "Dry run: would send '%s' to %s", subject, ", ".join(recipients)
        )
        return True

    sendgrid_api_key = getattr(settings, "SENDGRID_API_KEY", "")
    if sendgrid_api_key:
        return _send_via_sendgrid(sendgrid_api_key, recipients, subject, message, html_message=html_message)

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=recipients,
            fail_silently=False,
            html_message=html_message,
        )
        return True
    except Exception:
        logger.exception(
            "Failed to send email '%s' to %s", subject, ", ".join(recipients)
        )
        return False


def send_plumber_notification_email(subject, message, *, dry_run=False, html_message=None):
    recipients = get_plumber_notification_emails()
    if not recipients:
        logger.warning("No plumber notification email recipients configured.")
        return False

    if dry_run:
        logger.info(
            "Dry run: would send plumber notification email '%s' to %s",
            subject,
            ", ".join(recipients),
        )
        return True

    sendgrid_api_key = getattr(settings, "SENDGRID_API_KEY", "")
    if sendgrid_api_key:
        return _send_via_sendgrid(sendgrid_api_key, recipients, subject, message, html_message=html_message)

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=recipients,
            fail_silently=False,
            html_message=html_message,
        )
        return True
    except Exception:
        logger.exception(
            "Failed to send plumber notification email '%s' to %s",
            subject,
            ", ".join(recipients),
        )
        return False


def _send_via_sendgrid(api_key, recipients, subject, message, html_message=None):
    content = []
    if message:
        content.append({"type": "text/plain", "value": message})
    if html_message:
        content.append({"type": "text/html", "value": html_message})
    if not content:
        content = [{"type": "text/plain", "value": "(no content)"}]

    payload = {
        "personalizations": [
            {
                "to": [{"email": email} for email in recipients],
            }
        ],
        "from": {
            "email": getattr(settings, "SENDGRID_FROM_EMAIL", None)
            or getattr(settings, "DEFAULT_FROM_EMAIL", None),
        },
        "subject": subject,
        "content": content,
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
            subject,
            ", ".join(recipients),
            response.status_code,
            response.text,
        )
        return False
    except Exception:
        logger.exception(
            "Failed to send SendGrid email '%s' to %s",
            subject,
            ", ".join(recipients),
        )
        return False
