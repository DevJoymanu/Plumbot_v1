"""
Django management command: send_post_visit_followups
====================================================
Drives everything that happens after a site visit (see bot/post_visit.py for
the shape of the flow):

  * emails the plumber the debrief form 35 minutes after the visit ends, unless
    the in-app button already handled it;
  * starts the lead sequence at noon the next day when the form never came back
    (Case C), or hands the lead back when there is no email to write to;
  * sends the Case A confirmation two days before a job date the lead gave;
  * sends Case B asks 1, 2 and 3, then marks the lead cold and tells the plumber.

Every send is gated by a timestamp written the moment it goes out, so running
this on a frequent cron is safe:

    python manage.py send_post_visit_followups

It rides the existing Email_Follow_Ups service (PLUMBOT_CRON, every 5 minutes)
rather than needing a service of its own.
"""

from django.core.management.base import BaseCommand

from bot.post_visit import run_post_visit_tick


class Command(BaseCommand):
    help = 'Post-visit debrief chasing, quote follow-ups and the cold handback.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be sent without sending or writing.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY-RUN - nothing will be sent'))

        stats = run_post_visit_tick(dry_run=dry_run, log=lambda m: self.stdout.write(m))

        self.stdout.write(self.style.SUCCESS(
            'Post-visit -> form emails={form_emails} asks={asks} '
            'confirmations={confirmations} cold={cold} '
            'no-email={no_email} skipped={skipped}'.format(**stats)
        ))
