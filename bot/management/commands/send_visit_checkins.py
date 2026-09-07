"""
Django management command: send_visit_checkins
==============================================
The two check-ins for a future-dated site visit (see bot/visit_proposal.py):

  * at target minus 7 days and again at minus 4, ask the LEAD to confirm the
    day we penciled in, with a yes and a no link;
  * when there is no email on file, send those two to the PLUMBER instead,
    with a prefilled WhatsApp message they can send from their own phone;
  * give up quietly on a proposal nobody answered by the day itself.

Everything here is email by necessity, not preference. WhatsApp is free only
for 24 hours after the lead's last message, and this flow is measured in weeks.

Every send is gated by a timestamp written the moment it goes out, so running
this on a frequent cron is safe:

    python manage.py send_visit_checkins

It rides the existing Email_Follow_Ups service (PLUMBOT_CRON, every 5 minutes)
rather than needing one of its own.
"""

from django.core.management.base import BaseCommand

from bot.visit_proposal import run_visit_proposal_tick


class Command(BaseCommand):
    help = 'Confirm-the-visit check-ins for future-dated leads.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be sent without sending or writing.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY-RUN - nothing will be sent'))

        stats = run_visit_proposal_tick(dry_run=dry_run,
                                        log=lambda m: self.stdout.write(m))

        self.stdout.write(self.style.SUCCESS(
            'Visit check-ins -> lead={lead_emails} plumber={plumber_emails} '
            'lapsed={lapsed} skipped={skipped}'.format(**stats)
        ))
