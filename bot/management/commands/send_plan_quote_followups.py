"""
Django management command: send_plan_quote_followups
====================================================
Drives everything that happens after a lead sends a real plan (see
bot/plan_quote.py for the shape of the flow):

  * emails the plumber the job details and the plan an hour after it arrives,
    or as soon as the bot has the description, area and timeline;
  * chases the plumber at +2h, +4h and +8h until they answer the form;
  * follows the lead up about the quote, an hour after the plumber confirms,
    or at +12h on the assumption it went out if they never did.

Every send is gated by a timestamp written the moment it goes out, so running
this on a frequent cron is safe:

    python manage.py send_plan_quote_followups

It rides the existing Email_Follow_Ups service (PLUMBOT_CRON, every 5 minutes)
rather than needing a service of its own. Add it to that service's
comma-separated PLUMBOT_CRON value; do NOT give it a start command in the
Railway dashboard, which railway.json would override anyway.
"""

from django.core.management.base import BaseCommand

from bot.plan_quote import run_plan_quote_tick


class Command(BaseCommand):
    help = 'Plan-path plumber notification, reminders and quote follow-up.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be sent without sending or writing.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY-RUN - nothing will be sent'))

        stats = run_plan_quote_tick(dry_run=dry_run,
                                    log=lambda m: self.stdout.write(m))

        self.stdout.write(self.style.SUCCESS(
            'Plan quote -> plumber emails={plumber_emails} '
            'reminders={reminders} lead follow-ups={lead_followups} '
            'skipped={skipped}'.format(**stats)
        ))
