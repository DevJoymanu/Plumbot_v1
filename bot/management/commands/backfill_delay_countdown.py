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
        parser.add_argument(
            '--show-all',
            action='store_true',
            help='Print every appointment checked, not just updated ones.',
        )

    def _column_exists(self, column_name):
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = 'bot_appointment'
                  AND column_name = %s
            """, [column_name])
            return cursor.fetchone()[0] > 0

    def _raw_delay_rows(self):
        """
        Bypass the ORM entirely — raw SQL so no field-mapping issues.
        Finds every appointment whose internal_notes contains [DELAY_SIGNAL].
        """
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT id,
                       phone_number,
                       internal_notes,
                       last_customer_response,
                       updated_at,
                       is_delayed,
                       delay_signal_detected_at,
                       delay_followup_due_at
                FROM bot_appointment
                WHERE internal_notes LIKE %s
                ORDER BY id
            """, ['%[DELAY_SIGNAL]%'])
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def handle(self, *args, **options):
        dry_run  = options['dry_run']
        show_all = options['show_all']

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no changes will be saved.\n'))

        # ── 1. Confirm columns exist ──────────────────────────────────────────
        required = ['is_delayed', 'delay_signal_detected_at', 'delay_followup_due_at']
        missing  = [c for c in required if not self._column_exists(c)]
        if missing:
            self.stdout.write(self.style.ERROR(
                f"Missing columns in bot_appointment: {missing}\n"
                f"Run 'python manage.py migrate' first."
            ))
            return

        # ── 2. Diagnostic counts ──────────────────────────────────────────────
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM bot_appointment")
            total = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM bot_appointment WHERE internal_notes LIKE %s",
                ['%[DELAY_SIGNAL]%']
            )
            delay_count = cursor.fetchone()[0]

        self.stdout.write(f'Total appointments in DB    : {total}')
        self.stdout.write(f'With [DELAY_SIGNAL] in notes: {delay_count}')

        # ── 3. If nothing found, print a diagnostic sample ───────────────────
        if delay_count == 0:
            self.stdout.write(self.style.WARNING(
                '\nNo [DELAY_SIGNAL] text found. '
                'Printing a sample of non-empty internal_notes for inspection:'
            ))
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT id, phone_number, LEFT(COALESCE(internal_notes,''), 300)
                    FROM bot_appointment
                    WHERE internal_notes IS NOT NULL AND internal_notes != ''
                    ORDER BY updated_at DESC
                    LIMIT 20
                """)
                rows = cursor.fetchall()
            if rows:
                for row in rows:
                    self.stdout.write(f'  id={row[0]}  phone={row[1]}')
                    self.stdout.write(f'    {repr(row[2])}\n')
            else:
                self.stdout.write('  (no rows with non-empty internal_notes found)')
            return

        # ── 4. Process matching rows ──────────────────────────────────────────
        rows = self._raw_delay_rows()
        self.stdout.write(f'\nProcessing {len(rows)} appointment(s)...\n')

        updated = 0
        skipped = 0

        for row in rows:
            appt_id        = row['id']
            phone          = row['phone_number']
            already_done   = row['is_delayed'] and row['delay_followup_due_at']

            if already_done:
                skipped += 1
                if show_all:
                    self.stdout.write(f'  [SKIP] id={appt_id}  phone={phone} — already complete')
                continue

            # Best reference timestamp for when the delay started
            detected_at = (
                row['delay_signal_detected_at']
                or row['last_customer_response']
                or row['updated_at']
                or timezone.now()
            )
            due_at = detected_at + timedelta(days=14)

            if not dry_run:
                set_clauses = []
                params      = []

                if not row['is_delayed']:
                    set_clauses.append('is_delayed = TRUE')

                if not row['delay_signal_detected_at']:
                    set_clauses.append('delay_signal_detected_at = %s')
                    params.append(detected_at)

                if not row['delay_followup_due_at']:
                    set_clauses.append('delay_followup_due_at = %s')
                    params.append(due_at)

                if set_clauses:
                    params.append(appt_id)
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"UPDATE bot_appointment SET {', '.join(set_clauses)} WHERE id = %s",
                            params
                        )

            updated += 1
            self.stdout.write(
                f'  [{"DRY" if dry_run else "OK "}] '
                f'id={appt_id:<6} '
                f'phone={phone:<30} '
                f'detected={detected_at.strftime("%Y-%m-%d")} '
                f'due={due_at.strftime("%Y-%m-%d")}'
            )

        verb = 'Would update' if dry_run else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f'\n{verb} {updated} appointment(s). '
            f'Skipped {skipped} already-complete.'
        ))