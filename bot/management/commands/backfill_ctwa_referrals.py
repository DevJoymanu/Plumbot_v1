"""Backfill Appointment.ctwa_entry_at from stored WhatsAppInboundEvent referrals.

Ad referrals are captured on the inbound event (referral / raw_payload) the moment
they arrive, but a lead only gets ctwa_entry_at (and therefore the 72h window tag +
cadence) once handle_text_message promotes it. Any ad lead that messaged BEFORE that
promotion code was deployed has the referral on the event but no ctwa_entry_at on the
appointment. This command promotes those, using the event's own timestamp as the ad
entry time. Idempotent: never overrides an appointment that already has ctwa_entry_at.
"""
from django.core.management.base import BaseCommand
from bot.models import Appointment, WhatsAppInboundEvent


class Command(BaseCommand):
    help = 'Promote ad referrals from WhatsAppInboundEvent onto Appointment.ctwa_entry_at.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Preview changes without writing.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no changes will be saved.\n'))

        # Latest ad-referral event per sender = the current 72h window's entry point.
        # (A fresh ad click restarts the window, so the most recent one wins.)
        latest_by_sender = {}
        events = (
            WhatsAppInboundEvent.objects
            .filter(referral__isnull=False)
            .order_by('created_at')  # later events overwrite earlier -> latest wins
        )
        for ev in events:
            ref = ev.referral or {}
            if ref.get('source_type') != 'ad':
                continue
            if ev.sender:
                latest_by_sender[ev.sender] = ev

        self.stdout.write(f'Ad-referral senders found in events: {len(latest_by_sender)}')

        updated = skipped_set = skipped_missing = 0
        for sender, ev in latest_by_sender.items():
            ref = ev.referral or {}
            phone = f"whatsapp:+{sender}"
            appt = Appointment.objects.filter(phone_number=phone).first()

            if appt is None:
                skipped_missing += 1
                self.stdout.write(self.style.WARNING(
                    f'  [NO APPT] sender={sender} — no appointment for {phone}'
                ))
                continue

            if appt.ctwa_entry_at:
                skipped_set += 1
                self.stdout.write(
                    f'  [SKIP] appt={appt.id} sender={sender} — ctwa_entry_at already set'
                )
                continue

            source_id = str(ref.get('source_id') or '')[:64]
            self.stdout.write(
                f'  [{"DRY" if dry_run else "OK "}] appt={appt.id} sender={sender} '
                f'source_id={source_id} entry={ev.created_at:%Y-%m-%d %H:%M %Z}'
            )

            if not dry_run:
                appt.ctwa_source_id = source_id
                appt.ctwa_referral = ref
                appt.ctwa_entry_at = ev.created_at  # the real ad-click time, not now
                appt.save(update_fields=['ctwa_source_id', 'ctwa_referral', 'ctwa_entry_at'])
            updated += 1

        verb = 'Would update' if dry_run else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f'\n{verb} {updated} appointment(s). '
            f'Skipped {skipped_set} already set, {skipped_missing} with no appointment.'
        ))
