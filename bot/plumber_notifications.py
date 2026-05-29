import logging
import re
from email.utils import parseaddr

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
