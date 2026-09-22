"""
python manage.py preview_handoff [--tenant homebase] [--days 30] [--quoted]

Prints every message the handoff brief and the vague-date rule produce, for a
made-up lead, WITHOUT sending or saving anything.

WHY: the job-date ladder only fires days or weeks after a conversation, and
the test console's 999 numbers do not stop emails (the portfolio to the
address you type, the plumber's call brief, the no-email plumber alert), so a
real run on a test lead would email the plumber's inbox. This builds the lead
in memory (never saved) and calls the same functions the flow and the cron
call, so what it prints is what would go out.
"""

from datetime import date, timedelta
from unittest.mock import patch

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Preview the handoff and vague-date copy for a made-up lead. Sends nothing."

    def add_arguments(self, parser):
        parser.add_argument('--tenant', default='homebase',
                            help='Tenant slug whose plumber number and name to use.')
        parser.add_argument('--days', type=int, default=30,
                            help='How far out the lead\'s job date is (default 30).')
        parser.add_argument('--quoted', action='store_true',
                            help='Show only the B1 call brief (a quote was sent).')

    def _section(self, title, text):
        self.stdout.write(self.style.SUCCESS(f'\n=== {title} ==='))
        self.stdout.write(text or '(nothing: this tenant has no plumber number)')

    def handle(self, *args, **opts):
        from bot import job_date_ladder as ladder
        from bot import plumber_link
        from bot.models import Appointment, Tenant
        from bot.vague_dates import resolve

        tenant = Tenant.objects.filter(slug=opts['tenant']).first()
        job = date.today() + timedelta(days=opts['days'])
        # In memory only: never .save()d, so nothing reaches the database.
        lead = Appointment(
            tenant=tenant, customer_name='Rudo', project_type='bathroom_renovation',
            project_description='Full re-tile and new fittings',
            customer_area='Borrowdale', phone_number='whatsapp:+999000000001',
            customer_email='you@example.com',
            internal_notes=f'{ladder.JOB_DATE_TAG} {job.isoformat()}\n{ladder.STEP_TAG} 0',
        )

        self.stdout.write(f'Tenant: {getattr(tenant, "slug", "none (defaults)")}, '
                          f'job date {job:%A %d %B %Y}')
        if ladder.applies(job):
            self._section('Delay reply: follow-up permission (job - 7)',
                          ladder.permission_ask(ladder.first_followup_date(job)))
        else:
            self._section('Delay reply', 'Job date is a week or less out: the ladder '
                          'does not arm, the booking pivot runs instead.')
        self._section('Plumber link offer (sent with the portfolio)',
                      plumber_link.quote_offer(lead))
        self._section('Second follow-up: the handoff (delay-signal and three-field leads)',
                      plumber_link.handoff_message(lead))
        self._section('The link on its own (the plumber\'s short link once one is set)',
                      plumber_link.quote_link(lead))
        self._section(f'Touch at job - 7 ({ladder.step_day(job, 0):%a %d %b})',
                      ladder.touch_message(lead, ladder.STEP_FIRST))
        self._section(f'Touch at job - 3 ({ladder.step_day(job, 1):%a %d %b})',
                      ladder.touch_message(lead, ladder.STEP_SECOND))
        for quoted in ((True,) if opts['quoted'] else (True, False)):
            with patch.object(ladder, 'quote_given', return_value=quoted):
                subject, body, _html = ladder.call_brief(lead)
            self._section(f'Plumber call brief at job - 2 (quote sent: {quoted})',
                          f'Subject: {subject}\n\n{body}')

        self.stdout.write(self.style.SUCCESS('\n=== Vague timeframes: the date we assume ==='))
        for phrase in ('month end', 'mid next week', 'early next week', 'end of next week',
                       'early next month', 'mid month', 'in a few weeks', 'after payday',
                       'end of the year', 'soon'):
            frame = resolve(phrase)
            self.stdout.write(f'{phrase!r:22} -> {frame.anchor:%A %d %B}  '
                              f'(range {frame.start:%d %b} to {frame.end:%d %b})')
