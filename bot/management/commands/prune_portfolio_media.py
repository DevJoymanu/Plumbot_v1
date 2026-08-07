"""Delete orphaned portfolio uploads — files sitting in a tenant's media
folder that no TenantPortfolioItem points at.

Why they happen: gallery_upload stores the file (and charges the 20-file
quota) the moment it lands, but the item row is only created later by
gallery_finalize. If the owner closes the tab, or finalize rejects the batch
(it 400s on the first entry with no caption), the files stay in the bucket
with nothing referencing them. The tenant then reads "20 / 20 files used"
with an empty gallery and cannot upload anything more.

Nothing in the dashboard deletes a file that has no item row, so this is the
only way to reclaim that quota.

    python manage.py prune_portfolio_media --tenant hps --dry-run
    python manage.py prune_portfolio_media --tenant hps

Dry-run by default is deliberate: this deletes media permanently, so the
listing is shown first and --commit is required to act.
"""
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError

from bot.media_library import tenant_prefix
from bot.models import Tenant, TenantPortfolioItem


class Command(BaseCommand):
    help = 'Delete portfolio media files that no gallery item references.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', help='Tenant slug (default: every tenant).')
        parser.add_argument('--commit', action='store_true',
                            help='Actually delete. Without it, only lists.')

    def handle(self, *args, **options):
        slug = options.get('tenant')
        tenants = Tenant.objects.all()
        if slug:
            tenants = tenants.filter(slug=slug)
            if not tenants.exists():
                raise CommandError(f'No tenant with slug {slug!r}.')

        for tenant in tenants:
            prefix = tenant_prefix(tenant)
            try:
                _dirs, files = default_storage.listdir(prefix)
            except (FileNotFoundError, NotADirectoryError, OSError):
                continue
            if not files:
                continue

            # Both columns matter: a before/after pair keeps its "before" shot
            # in pair_filename, and pruning that would break the pair.
            referenced = set()
            for item in TenantPortfolioItem.objects.filter(tenant=tenant):
                for value in (item.filename, item.pair_filename):
                    if value:
                        referenced.add(value.rsplit('/', 1)[-1])

            orphans = sorted(name for name in files if name not in referenced)
            self.stdout.write(
                f'{tenant.slug}: {len(files)} file(s), '
                f'{len(referenced)} referenced, {len(orphans)} orphaned')
            if not orphans:
                continue

            for name in orphans:
                path = f'{prefix}/{name}'
                if options['commit']:
                    try:
                        default_storage.delete(path)
                        self.stdout.write(f'  deleted {name}')
                    except Exception as exc:      # noqa: BLE001 - report, keep going
                        self.stderr.write(f'  FAILED {name}: {exc}')
                else:
                    self.stdout.write(f'  would delete {name}')

            if not options['commit']:
                self.stdout.write(self.style.WARNING(
                    '  dry run - re-run with --commit to delete'))
