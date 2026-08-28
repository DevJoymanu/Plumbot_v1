"""Read back Meta's own billing and delivery verdicts for outbound messages.

This is the free way to answer two questions no local test can reach, because
the 999 test path short-circuits before Meta ever sees a message:

  1. Does a CTWA lead's free-form window really last 72h, or only the standard
     24h from the customer's last message? `Appointment.messaging_window_closes_at`
     assumes the former; Meta's docs say the free-entry-point window governs
     PRICE while the customer service window governs PERMISSION, and the two are
     independent. Section 3 below settles it from real traffic.

  2. What will the October 1 2026 service-message charge actually cost us? Meta
     already reports `billable` on every status webhook, so the free/paid split
     is observable today, before the first invoice.

Both ride status webhooks we already receive. Collecting the data costs nothing
and sends nothing.

    python manage.py whatsapp_window_report --days 30 --rate 0.02
"""
from collections import Counter, defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from bot.models import WhatsAppSendCost

# Buckets are hours since the customer's last inbound message, which is what the
# customer service window is measured from. The 24h boundary is the one that
# matters: sends landing beyond it are the ones our CTWA schedule assumes will
# still go through.
BUCKETS = [(0, 24, '0-24h  (inside CSW)'),
           (24, 48, '24-48h (CSW shut, FEP open)'),
           (48, 72, '48-72h (CSW shut, FEP open)'),
           (72, None, '72h+   (both shut)')]

WINDOW_CLOSED_CODE = '131047'


def _bucket(hours):
    if hours is None:
        return None
    for low, high, label in BUCKETS:
        if hours >= low and (high is None or hours < high):
            return label
    return None


class Command(BaseCommand):
    help = "Report Meta's billing/delivery verdicts for outbound WhatsApp messages."

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30,
                            help='How far back to read (default 30).')
        parser.add_argument('--rate', type=float, default=0.02,
                            help='Per-message rate for the cost projection '
                                 '(default 0.02, the Rest of Africa utility rate).')

    def handle(self, *args, **options):
        days = options['days']
        rate = options['rate']
        since = timezone.now() - timedelta(days=days)
        rows = list(WhatsAppSendCost.objects.filter(created_at__gte=since))

        if not rows:
            self.stdout.write(self.style.WARNING(
                f'No send-cost rows in the last {days} days.\n'
                'Nothing is collected until a status webhook arrives after this '
                'feature is deployed. Send one real message and check again.'
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\nWhatsApp send verdicts — last {days} days ({len(rows)} messages)\n'))

        self._coverage(rows)
        self._billing(rows, rate)
        self._window_survival(rows)

    # -- 1. How much of the data actually carries a pricing verdict ------------
    def _coverage(self, rows):
        priced = [r for r in rows if r.billable is not None]
        self.stdout.write(self.style.MIGRATE_HEADING('1. Verdict coverage'))
        self.stdout.write(f'   messages seen        : {len(rows)}')
        self.stdout.write(f'   carrying pricing     : {len(priced)}')
        if not priced:
            self.stdout.write(self.style.WARNING(
                '   No pricing objects yet. Meta attaches pricing to delivered '
                'statuses; if this stays at 0, check the webhook is subscribed '
                'to message status events.'))
        self.stdout.write('')

    # -- 2. What Meta charges for ---------------------------------------------
    def _billing(self, rows, rate):
        priced = [r for r in rows if r.billable is not None]
        if not priced:
            return
        billable = [r for r in priced if r.billable]
        free = [r for r in priced if not r.billable]

        self.stdout.write(self.style.MIGRATE_HEADING('2. Billing split (today)'))
        self.stdout.write(f'   free                 : {len(free)}')
        self.stdout.write(f'   billable             : {len(billable)}')

        kinds = Counter(
            f'{r.pricing_type or "?"}/{r.category or "?"}' for r in priced)
        for kind, n in kinds.most_common():
            self.stdout.write(f'     {kind:<34} {n}')

        # After Oct 1 2026 the free_customer_service rows become chargeable.
        # Free-entry-point rows stay free, so they are excluded from the estimate.
        would_bill = [r for r in free
                      if 'customer_service' in (r.pricing_type or '')]
        per_day = len(would_bill) / max(len(set(r.created_at.date() for r in rows)), 1)
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            '   Projection once service messages become chargeable'))
        self.stdout.write(f'   currently-free service msgs : {len(would_bill)}')
        self.stdout.write(f'   at ${rate:.4f} each          : ${len(would_bill) * rate:,.2f} '
                          f'over this period')
        self.stdout.write(f'   run rate                    : ${per_day * 30 * rate:,.2f} / 30 days')
        self.stdout.write('')

    # -- 3. The CTWA window question ------------------------------------------
    def _window_survival(self, rows):
        self.stdout.write(self.style.MIGRATE_HEADING(
            '3. Does free-form sending survive past the 24h customer service window?'))
        self.stdout.write(
            '   Delivered past 24h means our 72h CTWA assumption holds.\n'
            '   131047 past 24h means it does not, and CTWA follow-ups 4-6 are bouncing.\n')

        for is_ctwa, label in ((True, 'CTWA (ad) leads'), (False, 'Organic leads')):
            group = [r for r in rows if r.was_ctwa_lead is is_ctwa]
            if not group:
                continue
            self.stdout.write(f'   {label}:')
            tally = defaultdict(Counter)
            for r in group:
                b = _bucket(r.hours_since_last_inbound)
                if b is None:
                    continue
                if WINDOW_CLOSED_CODE in (r.error_codes or ''):
                    tally[b]['bounced_131047'] += 1
                elif r.status in ('delivered', 'read'):
                    tally[b]['delivered'] += 1
                elif r.status == 'failed':
                    tally[b]['failed_other'] += 1
                else:
                    tally[b]['sent_only'] += 1

            for _, _, bucket_label in BUCKETS:
                counts = tally.get(bucket_label)
                if not counts:
                    continue
                detail = '  '.join(f'{k}={v}' for k, v in sorted(counts.items()))
                self.stdout.write(f'     {bucket_label:<28} {detail}')
            self.stdout.write('')
