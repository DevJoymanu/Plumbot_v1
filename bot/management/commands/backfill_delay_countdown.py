from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from django.db import connection


class Command(BaseCommand):
    help = 'Backfill is_delayed / delay_followup_due_at for existing appointments.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview changes without writing to the database.',
        )

    def _column_exists(self, column_name):
        """Check whether a column exists in bot_appointment before querying it."""
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = 'bot_appointment'
                  AND column_name = %s
            """, [column_name])
            return cursor.fetchone()[0] > 0

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no changes will be saved.'))

        # Guard: make sure the columns we need actually exist
        required_columns = ['is_delayed', 'delay_signal_detected_at', 'delay_followup_due_at']
        missing = [c for c in required_columns if not self._column_exists(c)]
        if missing:
            self.stdout.write(self.style.ERROR(
                f"Missing columns in bot_appointment: {missing}\n"
                f"Run 'python manage.py migrate' first, then re-run this command."
            ))
            return

        # Safe to import Appointment now that columns exist
        from bot.models import Appointment

        qs = Appointment.objects.filter(
            internal_notes__contains='[DELAY_SIGNAL]'
        ).only(
            'id',
            'phone_number',
            'internal_notes',
            'is_delayed',
            'delay_signal_detected_at',
            'delay_followup_due_at',
            'last_customer_response',
            'updated_at',
        )

        total = qs.count()
        self.stdout.write(f'Found {total} appointment(s) with [DELAY_SIGNAL].')

        updated = 0
        skipped = 0

        for a in qs:
            already_complete = a.is_delayed and a.delay_followup_due_at
            if already_complete:
                skipped += 1
                continue

            detected_at = (
                a.delay_signal_detected_at
                or a.last_customer_response
                or a.updated_at
                or timezone.now()
            )
            due_at = detected_at + timedelta(days=14)

            if not dry_run:
                update_fields = []
                if not a.is_delayed:
                    a.is_delayed = True
                    update_fields.append('is_delayed')
                if not a.delay_signal_detected_at:
                    a.delay_signal_detected_at = detected_at
                    update_fields.append('delay_signal_detected_at')
                if not a.delay_followup_due_at:
                    a.delay_followup_due_at = due_at
                    update_fields.append('delay_followup_due_at')
                if update_fields:
                    a.save(update_fields=update_fields)

            updated += 1
            self.stdout.write(
                f'  [{"DRY" if dry_run else "OK "}] '
                f'id={a.id:<6} '
                f'phone={a.phone_number:<30} '
                f'detected={detected_at.strftime("%Y-%m-%d")} '
                f'due={due_at.strftime("%Y-%m-%d")}'
            )

        verb = 'Would update' if dry_run else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f'\n{verb} {updated} appointment(s). '
            f'Skipped {skipped} already-complete.'
        ))