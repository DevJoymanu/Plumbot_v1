"""Fill TenantPortfolioItem.vision_description for photos already in a gallery.

New uploads are described when they're added (bot/views/gallery.py), so this is
the one-off backfill for everything uploaded before that existed. Idempotent:
a row that already has a description is skipped, so it is safe to re-run and
safe to interrupt.

    python manage.py describe_portfolio_photos --dry-run
    python manage.py describe_portfolio_photos --tenant barmak-plumbing
"""
from django.core.management.base import BaseCommand

from bot.media_library import describe_portfolio_item, is_video_filename
from bot.models import TenantPortfolioItem


class Command(BaseCommand):
    help = "Describe existing gallery photos with vision (one-off backfill)."

    def add_arguments(self, parser):
        parser.add_argument('--tenant', default='',
                            help='Limit to one tenant slug (default: all).')
        parser.add_argument('--limit', type=int, default=0,
                            help='Stop after N photos (default: no limit).')
        parser.add_argument('--dry-run', action='store_true',
                            help='List what would be described, call nothing.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        rows = TenantPortfolioItem.objects.filter(
            is_active=True, vision_description='').select_related('tenant')
        if options['tenant']:
            rows = rows.filter(tenant__slug=options['tenant'])
        rows = [r for r in rows.order_by('tenant_id', 'pk')
                if r.filename and not is_video_filename(r.filename)]
        if options['limit']:
            rows = rows[:options['limit']]

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — describing nothing.\n'))
        self.stdout.write(f'Photos without a description: {len(rows)}')

        done = failed = 0
        for item in rows:
            label = f'{item.tenant.slug} · {item.item_id}'
            if dry_run:
                self.stdout.write(f'  [WOULD DESCRIBE] {label}')
                continue
            description = describe_portfolio_item(item)
            if description:
                done += 1
                self.stdout.write(self.style.SUCCESS(
                    f'  [OK] {label}: {description[:90]}'))
            else:
                failed += 1
                self.stdout.write(self.style.WARNING(
                    f'  [SKIP] {label} — vision returned nothing'))

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(
                f'\nDescribed {done}, skipped {failed}.'))
