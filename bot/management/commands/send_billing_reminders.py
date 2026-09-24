"""
Run the platform billing reminders by hand (bot/billing_reminders.py).

The scheduled run rides on `send_scheduled_followups` (the Email_Follow_Ups
cron), so this command is for checking what is due (`--dry-run`) or catching
up after an outage. It is safe to run any number of times: each step is
logged on its invoice and never sent twice.
"""

from django.core.management.base import BaseCommand

from bot.billing_reminders import run_billing_reminders


class Command(BaseCommand):
    help = 'Send due platform billing reminders (tenant reminders, payment checks, switch-off notices).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='List what is due without sending anything.')

    def handle(self, *args, **options):
        res = run_billing_reminders(dry_run=options['dry_run'],
                                    log=lambda m: self.stdout.write(m))
        self.stdout.write(f"Billing reminders: due={res['due']} sent={res['sent']} failed={res['failed']}")
