from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from bot.models import Appointment


class Command(BaseCommand):
    help = 'Backfill is_delayed / delay_followup_due_at for existing appointments.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview changes without writing to the database.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no changes will be saved.'))

        qs = Appointment.objects.filter(internal_notes__contains='[DELAY_SIGNAL]')
        self.stdout.write(f'Found {qs.count()} appointment(s) with [DELAY_SIGNAL].')

        updated = 0
        skipped = 0

        for a in qs:
            already_complete = a.is_delayed and a.delay_followup_due_at
            if already_complete:
                skipped += 1
                continue

            # Pick the best reference timestamp for "when the delay started"
            detected_at = (
                a.delay_signal_detected_at
                or a.last_customer_response
                or a.updated_at
                or timezone.now()
            )

            if not dry_run:
                a.is_delayed = True
                if not a.delay_signal_detected_at:
                    a.delay_signal_detected_at = detected_at
                if not a.delay_followup_due_at:
                    a.delay_followup_due_at = detected_at + timedelta(days=14)
                a.save(update_fields=[
                    'is_delayed',
                    'delay_signal_detected_at',
                    'delay_followup_due_at',
                ])

            updated += 1
            self.stdout.write(
                f'  [{"DRY" if dry_run else "OK"}] '
                f'appointment={a.id} '
                f'phone={a.phone_number} '
                f'detected_at={detected_at.strftime("%Y-%m-%d")} '
                f'due={( detected_at + timedelta(days=14)).strftime("%Y-%m-%d")}'
            )

        verb = 'Would update' if dry_run else 'Updated'
        self.stdout.write(
            self.style.SUCCESS(
                f'{verb} {updated} appointment(s). Skipped {skipped} already-complete.'
            )
        )