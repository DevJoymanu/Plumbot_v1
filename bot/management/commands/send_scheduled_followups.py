"""
Django management command: send_scheduled_followups
===================================================
Dispatches staff-queued follow-ups (bot.models.ScheduledFollowup) whose
scheduled_for time has arrived. Sends via WhatsApp or email, logs the send onto
the appointment's conversation history (so it shows in the follow-up tabs), and
marks each row sent / failed.

Run on a frequent cron (e.g. every 5 minutes):

    python manage.py send_scheduled_followups

It is ALSO invoked at the start of ``send_followups`` so scheduled follow-ups go
out on that cron's cadence without needing a separate schedule entry.
"""

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


def dispatch_due_scheduled_followups(now=None, dry_run=False, log=None):
    """Send every pending ScheduledFollowup whose time has come.

    Returns {'sent': int, 'failed': int}. ``log`` is an optional callable used
    to emit human-readable progress lines (e.g. command stdout writer).
    """
    from bot.models import ScheduledFollowup

    now = now or timezone.now()

    def _emit(msg):
        if log:
            log(msg)

    due = (
        ScheduledFollowup.objects
        .filter(status='pending', scheduled_for__lte=now)
        .select_related('appointment')
    )

    sent = failed = 0
    for sf in due:
        apt = sf.appointment
        name = apt.customer_name or 'there'
        try:
            if sf.channel == 'whatsapp':
                message = (sf.message or '').replace('{name}', name)
                if dry_run:
                    _emit(f'[dry-run] WhatsApp → apt {apt.pk}: {message[:60]}…')
                else:
                    from bot.whatsapp_cloud_api import whatsapp_api
                    from bot.utils import clean_phone_number
                    whatsapp_api.send_text_message(clean_phone_number(apt.phone_number), message)
                    apt.add_conversation_message('assistant', f'[SCHEDULED FOLLOW-UP] {message}')
                    apt.last_followup_sent = timezone.now()
                    apt.followup_count = (apt.followup_count or 0) + 1
                    apt.save(update_fields=['last_followup_sent', 'followup_count'])
            else:  # email
                if not apt.customer_email:
                    raise ValueError('no customer_email on appointment')

                if sf.template_key:
                    # Render the catalogue template fresh at send time so dates
                    # and conversation context are current.
                    from bot.email_catalog import EMAIL_CATALOG
                    entry = EMAIL_CATALOG.get(sf.template_key)
                    if not entry:
                        raise ValueError(f'unknown email template {sf.template_key!r}')
                    label = entry['label']
                    if dry_run:
                        _emit(f'[dry-run] Email (template {sf.template_key}) → {apt.customer_email}')
                    else:
                        ok = entry['send'](apt)
                        if not ok:
                            raise RuntimeError('templated email send returned False')
                        apt.add_conversation_message('assistant', f'[SCHEDULED EMAIL] {label} sent to customer')
                else:
                    body = (sf.message or '').replace('{name}', name)
                    subject = sf.subject or 'Following up'
                    if dry_run:
                        _emit(f'[dry-run] Email → {apt.customer_email}: {subject}')
                    else:
                        from bot.customer_emails import _wrap, _send
                        paragraphs = ''.join(
                            f'<p>{line.strip()}</p>' for line in body.split('\n') if line.strip()
                        )
                        ok = _send(apt, subject, _wrap(paragraphs))
                        if not ok:
                            raise RuntimeError('email send returned False')
                        apt.add_conversation_message('assistant', f'[SCHEDULED EMAIL] {subject}: {body}')

            if not dry_run:
                sf.status = 'sent'
                sf.sent_at = timezone.now()
                sf.error = ''
                sf.save(update_fields=['status', 'sent_at', 'error'])
            sent += 1
            _emit(f'✅ sent scheduled {sf.channel} follow-up #{sf.id} (apt {apt.pk})')

        except Exception as exc:  # noqa: BLE001 — record every failure on the row
            failed += 1
            if not dry_run:
                sf.status = 'failed'
                sf.error = f'{type(exc).__name__}: {exc}'
                sf.save(update_fields=['status', 'error'])
            _emit(f'❌ failed scheduled {sf.channel} follow-up #{sf.id}: {exc}')
            logger.warning('Scheduled follow-up %s failed: %s', sf.id, exc)

    return {'sent': sent, 'failed': failed}


class Command(BaseCommand):
    help = 'Dispatch staff-scheduled follow-ups that are now due (WhatsApp + email).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be sent without sending.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('🧪 DRY-RUN — no messages will be sent'))
        res = dispatch_due_scheduled_followups(
            dry_run=dry_run, log=lambda m: self.stdout.write(m)
        )
        self.stdout.write(self.style.SUCCESS(
            f"Scheduled follow-ups → sent={res['sent']}  failed={res['failed']}"
        ))
