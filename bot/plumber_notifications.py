import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

DEFAULT_PLUMBER_NOTIFICATION_EMAILS = [
    "jones86xi@gmail.com",
    "info@homebaseplumbers.co.zw",
]


def get_plumber_notification_emails():
    recipients = getattr(
        settings,
        "PLUMBER_NOTIFICATION_EMAILS",
        DEFAULT_PLUMBER_NOTIFICATION_EMAILS,
    )
    return [email for email in recipients if email]


def send_plumber_notification_email(subject, message, *, dry_run=False):
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

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=recipients,
            fail_silently=False,
        )
        return True
    except Exception:
        logger.exception(
            "Failed to send plumber notification email '%s' to %s",
            subject,
            ", ".join(recipients),
        )
        return False
