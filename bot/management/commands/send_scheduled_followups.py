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
                    from bot.whatsapp_cloud_api import get_client_for_tenant
                    from bot.utils import clean_phone_number
                    get_client_for_tenant(getattr(sf.appointment, 'tenant', None)).send_text_message(clean_phone_number(apt.phone_number), message)
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
                        ok = _send(apt, subject, _wrap(paragraphs), category='followup')
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

        # Is the follow-up cron (a separate Railway service) still ticking?
        # One email to the operator if it has gone quiet inside the sending
        # hours (bot/cron_health.py). Checked from THIS service on purpose: a
        # dead cron cannot report its own death.
        try:
            from bot.cron_health import alert_if_stale
            if not dry_run and alert_if_stale():
                self.stdout.write('Follow-up cron is not running: operator alerted')
        except Exception as exc:  # noqa: BLE001
            logger.warning('Follow-up heartbeat check failed: %s', exc)

        # The hourly check for a lead message that never got a reply
        # (bot/unanswered_sweep.py): a reply prepared inside the web server
        # dies with it on a deploy or restart. This cron ticks every 5
        # minutes; only the first tick of each hour sweeps. Own try, like the
        # reminders below.
        try:
            from bot.unanswered_sweep import answer_unanswered, is_sweep_tick
            if is_sweep_tick():
                ures = answer_unanswered(dry_run=dry_run, log=lambda m: self.stdout.write(m))
                if any(ures.values()):
                    self.stdout.write(
                        f"Unanswered sweep → answered={ures['answered']} failed={ures['failed']}")
        except Exception as exc:  # noqa: BLE001
            logger.warning('Unanswered sweep failed: %s', exc)

        # The 24-hour reminder for an unlogged "please call this lead" email
        # (bot/call_brief.py). Here, in the command and NOT in
        # dispatch_due_scheduled_followups, because that function also runs
        # from send_followups on its own cron: two crons ticking the same
        # minute could each send the reminder before either logged it. Its
        # own try, so a failure here never hides the follow-ups above.
        try:
            from bot.call_brief import send_due_reminders
            cres = send_due_reminders(dry_run=dry_run, log=lambda m: self.stdout.write(m))
            if any(cres.values()):
                self.stdout.write(
                    f"Call reminders → sent={cres['sent']} skipped={cres['skipped']} "
                    f"failed={cres['failed']}")
        except Exception as exc:  # noqa: BLE001
            logger.warning('Call reminders failed: %s', exc)

        # Platform billing reminders (bot/billing_reminders.py): tenant
        # reminders before and on the due date, the operator's due-day payment
        # check, and the not-paid switch-off countdown. Ridden on this 5-minute
        # cron so billing needs no Railway service or PLUMBOT_CRON entry; each
        # step is logged on the invoice, so every later tick the same day is a
        # no-op. Its own try, like everything above.
        try:
            from bot.billing_reminders import run_billing_reminders
            bres = run_billing_reminders(dry_run=dry_run, log=lambda m: self.stdout.write(m))
            if any(bres.values()):
                self.stdout.write(
                    f"Billing reminders → due={bres['due']} sent={bres['sent']} "
                    f"failed={bres['failed']}")
        except Exception as exc:  # noqa: BLE001
            logger.warning('Billing reminders failed: %s', exc)
