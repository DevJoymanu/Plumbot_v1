"""
Django management command: send_phone_quote_followups
=====================================================
Follows up the customer after the plumber quoted them over the phone (see
bot/phone_quote.py for the shape of the flow).

Shorter than the plan-path job, and deliberately so. That one spends most of
its cadence getting hold of the plumber; here the plumber filled the form in
themselves while the customer was on the line, so the only thing left is to ask
the customer about the quote an hour later.

Every send is gated by a timestamp written the moment it goes out, so running
this on a frequent cron is safe:

    python manage.py send_phone_quote_followups

It rides the existing Email_Follow_Ups service (PLUMBOT_CRON, every 5 minutes)
rather than needing a service of its own. Add it to that service's
comma-separated PLUMBOT_CRON value; do NOT give it a start command in the
Railway dashboard, which railway.json would override anyway.
"""

from django.core.management.base import BaseCommand

from bot.phone_quote import run_phone_quote_tick


class Command(BaseCommand):
    help = 'Customer follow-up after a quote given over the phone.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be sent without sending or writing.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY-RUN - nothing will be sent'))

        stats = run_phone_quote_tick(dry_run=dry_run,
                                     log=lambda m: self.stdout.write(m))

        self.stdout.write(self.style.SUCCESS(
            'Phone quote -> lead follow-ups={lead_followups} '
            'skipped={skipped} errors={errors}'.format(**stats)
        ))
