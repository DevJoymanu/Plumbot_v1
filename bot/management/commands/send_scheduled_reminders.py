"""
Django management command: send_scheduled_reminders
===================================================
Dispatches staff-queued reminders (bot.models.ScheduledReminder) whose
scheduled_for time has arrived. The recipient is per-reminder:

  • target='customer' → WhatsApp / email to the lead (like a scheduled follow-up).
  • target='plumber'  → email to the plumber notification inboxes, or WhatsApp to
                        PLUMBER_PHONE_NUMBER.

Marks each row sent / failed. Run on a frequent cron (e.g. every 5 minutes); it
is ALSO invoked at the start of ``send_reminders`` so it goes out on that cadence.

    python manage.py send_scheduled_reminders
"""

import logging
import os

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


def _plumber_html(subject, body):
    """Minimal HTML wrap for a plumber reminder email."""
    paragraphs = ''.join(
        f'<p style="margin:0 0 12px;">{line.strip()}</p>'
        for line in body.split('\n') if line.strip()
    )
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;'
        'margin:0 auto;padding:20px;color:#1f2937;">'
        f'<h2 style="font-size:18px;color:#0f766e;margin:0 0 14px;">{subject}</h2>'
        f'{paragraphs}'
        '<p style="font-size:12px;color:#94a3b8;margin-top:18px;">'
        'Scheduled reminder from Plumbot.</p>'
        '</div>'
    )


def dispatch_due_scheduled_reminders(now=None, dry_run=False, log=None):
    """Send every pending ScheduledReminder whose time has come.

    Returns {'sent': int, 'failed': int}. ``log`` is an optional callable used to
    emit human-readable progress lines (e.g. command stdout writer).
    """
    from bot.models import ScheduledReminder

    now = now or timezone.now()

    def _emit(msg):
        if log:
            log(msg)

    due = (
        ScheduledReminder.objects
        .filter(status='pending', scheduled_for__lte=now)
        .select_related('appointment')
    )

    sent = failed = 0
    for r in due:
        apt = r.appointment
        name = apt.customer_name or 'there'
        body = (r.message or '').replace('{name}', name)
        try:
            if r.target == 'customer':
                if r.channel == 'whatsapp':
                    if dry_run:
                        _emit(f'[dry-run] customer WhatsApp → apt {apt.pk}: {body[:60]}…')
                    else:
                        from bot.whatsapp_cloud_api import whatsapp_api
                        from bot.utils import clean_phone_number
                        whatsapp_api.send_text_message(clean_phone_number(apt.phone_number), body)
                        apt.add_conversation_message('assistant', f'[SCHEDULED REMINDER] {body}')
                else:  # email
                    if not apt.customer_email:
                        raise ValueError('no customer_email on appointment')
                    if dry_run:
                        _emit(f'[dry-run] customer Email → {apt.customer_email}: {r.subject or "Reminder"}')
                    else:
                        from bot.customer_emails import _wrap, _send
                        paragraphs = ''.join(
                            f'<p>{line.strip()}</p>' for line in body.split('\n') if line.strip()
                        )
                        ok = _send(apt, r.subject or 'Reminder', _wrap(paragraphs))
                        if not ok:
                            raise RuntimeError('customer email send returned False')
                        apt.add_conversation_message(
                            'assistant', f'[SCHEDULED REMINDER EMAIL] {r.subject}: {body}'
                        )
            else:  # target == 'plumber'
                if r.channel == 'email':
                    from bot.plumber_notifications import (
                        get_plumber_notification_emails, send_email_to_recipients,
                    )
                    recipients = get_plumber_notification_emails()
                    if not recipients:
                        raise ValueError('no plumber notification emails configured')
                    subject = r.subject or f'Reminder — {name}'
                    if dry_run:
                        _emit(f'[dry-run] plumber Email → {recipients}: {subject}')
                    else:
                        ok = send_email_to_recipients(
                            recipients, subject, body, html_message=_plumber_html(subject, body),
                        )
                        if not ok:
                            raise RuntimeError('plumber email send returned False')
                else:  # whatsapp to plumber
                    plumber_phone = os.environ.get('PLUMBER_PHONE_NUMBER', '').replace('+', '').strip()
                    if not plumber_phone:
                        raise ValueError('PLUMBER_PHONE_NUMBER env var not set')
                    if dry_run:
                        _emit(f'[dry-run] plumber WhatsApp → +{plumber_phone}: {body[:60]}…')
                    else:
                        from bot.whatsapp_cloud_api import whatsapp_api
                        whatsapp_api.send_text_message(plumber_phone, body)

            if not dry_run:
                r.status = 'sent'
                r.sent_at = timezone.now()
                r.error = ''
                r.save(update_fields=['status', 'sent_at', 'error'])
            sent += 1
            _emit(f'✅ sent scheduled {r.target} {r.channel} reminder #{r.id} (apt {apt.pk})')

        except Exception as exc:  # noqa: BLE001 — record every failure on the row
            failed += 1
            if not dry_run:
                r.status = 'failed'
                r.error = f'{type(exc).__name__}: {exc}'
                r.save(update_fields=['status', 'error'])
            _emit(f'❌ failed scheduled reminder #{r.id}: {exc}')
            logger.warning('Scheduled reminder %s failed: %s', r.id, exc)

    return {'sent': sent, 'failed': failed}


class Command(BaseCommand):
    help = 'Dispatch staff-scheduled reminders that are now due (customer/plumber, WhatsApp/email).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be sent without sending.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('🧪 DRY-RUN — no messages will be sent'))
        res = dispatch_due_scheduled_reminders(
            dry_run=dry_run, log=lambda m: self.stdout.write(m)
        )
        self.stdout.write(self.style.SUCCESS(
            f"Scheduled reminders → sent={res['sent']}  failed={res['failed']}"
        ))
