"""Repair the leads flattened by the unfiltered job-scheduling update.

bot/views/jobs.schedule_job used to write one job with
`Appointment.objects.update(...)` — a MANAGER-level update, so it carried no
filter and rewrote every Appointment row in every tenant with that one job's
values:

    customer_name, customer_email, customer_area, project_type, property_type,
    project_description, scheduled_datetime, appointment_type='job',
    status='scheduled', has_plan, timeline

The visible symptom is every lead sitting in VERY HOT: calculate_lead_score
returns 100/VERY_HOT for any lead with a `scheduled_datetime`, and the whole
table got one.

What this command can put back, deterministically:

  * `scheduled_datetime` — `.update()` bypasses Model.save(), so `end_datetime`
    was NOT touched. save() sets `end_datetime = scheduled_datetime + duration`
    whenever a datetime is written, so `end_datetime - duration` IS the
    original appointment time, and a NULL `end_datetime` proves the lead never
    had one (clear it -> the lead drops out of VERY HOT).
  * `status` — `booked_at` was not touched either, and save() stamps it the
    first time a lead becomes 'confirmed'. booked_at set -> the lead had been
    confirmed; unset -> it had never been booked, so 'pending'.
  * `appointment_type` — back to 'site_visit' ('job' is not even a member of
    APPOINTMENT_TYPE_CHOICES; only the broken update could write it).
  * `lead_score` / `lead_status` — recomputed from the repaired fields.

What NOTHING in the database can put back: customer_name, customer_email,
customer_area, project_type, property_type, project_description, has_plan and
timeline. Every affected row holds the same smeared value; the originals are
gone. Restore those from a database backup taken before the incident — this
command prints the smeared values so you can recognise them.

    python manage.py repair_mass_job_update            # report only
    python manage.py repair_mass_job_update --apply    # write the repair
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from bot.models import Appointment
from bot.services.lead_scoring import refresh_lead_score

# Neither value is a member of its field's choices, so only the broken
# manager-level update can have written them.
IMPOSSIBLE_APPOINTMENT_TYPE = 'job'
IMPOSSIBLE_STATUS = 'scheduled'

SMEARED_FIELDS = [
    'customer_name', 'customer_email', 'customer_area', 'project_type',
    'property_type', 'project_description', 'has_plan', 'timeline',
]


class Command(BaseCommand):
    help = 'Repair leads flattened by the unfiltered job-scheduling update.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the repair. Without it the command only reports.',
        )

    def handle(self, *args, **options):
        apply_changes = options['apply']

        damaged = Appointment.objects.filter(
            appointment_type=IMPOSSIBLE_APPOINTMENT_TYPE,
        ) | Appointment.objects.filter(status=IMPOSSIBLE_STATUS)
        damaged = damaged.distinct().order_by('pk')

        total = damaged.count()
        if not total:
            self.stdout.write(self.style.SUCCESS(
                'No rows carry the impossible appointment_type=%r / status=%r. '
                'Nothing to repair.' % (IMPOSSIBLE_APPOINTMENT_TYPE, IMPOSSIBLE_STATUS)
            ))
            return

        self.stdout.write(self.style.WARNING(
            '%d appointment row(s) were hit by the unfiltered update.' % total
        ))

        # The smeared values are identical across rows — show them so the
        # operator can tell repaired rows from untouched ones in a backup.
        self.stdout.write('\nValues the update smeared across every row '
                          '(NOT recoverable from the database):')
        for field in SMEARED_FIELDS:
            counts = Counter(getattr(row, field) for row in damaged)
            shown = ', '.join(
                '%r x%d' % (value, count) for value, count in counts.most_common(3)
            )
            self.stdout.write('  %-22s %s' % (field + ':', shown))

        restored_time = cleared_time = confirmed = pending = 0
        tenants = Counter()

        for lead in damaged:
            tenants[getattr(lead.tenant, 'slug', None) or 'none'] += 1

            if lead.end_datetime:
                lead.scheduled_datetime = lead.end_datetime - lead.duration
                restored_time += 1
            else:
                lead.scheduled_datetime = None
                cleared_time += 1

            if lead.booked_at:
                lead.status = 'confirmed'
                confirmed += 1
            else:
                lead.status = 'pending'
                pending += 1

            lead.appointment_type = 'site_visit'

            if apply_changes:
                with transaction.atomic():
                    lead.save(update_fields=[
                        'scheduled_datetime', 'end_datetime', 'status',
                        'appointment_type', 'booked_at',
                    ])
                    refresh_lead_score(lead)

        self.stdout.write('\nRepair plan:')
        self.stdout.write('  scheduled_datetime restored from end_datetime: %d' % restored_time)
        self.stdout.write('  scheduled_datetime cleared (lead never had one): %d' % cleared_time)
        self.stdout.write('  status -> confirmed (booked_at was stamped):    %d' % confirmed)
        self.stdout.write('  status -> pending  (never booked):              %d' % pending)
        self.stdout.write('  appointment_type -> site_visit:                 %d' % total)
        self.stdout.write('  tenants affected: %s' % dict(tenants))

        self.stdout.write(self.style.WARNING(
            '\nstatus is inferred from booked_at, so a lead that was confirmed '
            'and later cancelled or completed comes back as "confirmed" — '
            'check those by hand.'
        ))

        if apply_changes:
            self.stdout.write(self.style.SUCCESS(
                '\nRepaired %d row(s) at %s. The smeared identity fields above '
                'still need a backup restore.' % (total, timezone.now().isoformat())
            ))
        else:
            self.stdout.write(self.style.NOTICE(
                '\nDRY RUN — nothing written. Re-run with --apply to commit.'
            ))
