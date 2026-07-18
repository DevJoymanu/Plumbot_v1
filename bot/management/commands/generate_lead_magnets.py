"""Build + cache every tenant's lead-magnet PDF into object storage.

Run once to populate lead_magnets_pdfs/<slug>/ for existing tenants (new/edited
tenants regenerate automatically on config save). Safe to re-run.

    python manage.py generate_lead_magnets            # only missing
    python manage.py generate_lead_magnets --force    # rebuild all
    python manage.py generate_lead_magnets --tenant acme
"""
from django.core.management.base import BaseCommand

from bot.lead_magnet import design_for, get_or_build_lead_magnet, storage_path
from bot.models import Tenant


class Command(BaseCommand):
    help = "Generate and cache each tenant's lead-magnet PDF in object storage."

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                            help='Rebuild even if a cached PDF already exists.')
        parser.add_argument('--tenant', help='Limit to one tenant slug.')

    def handle(self, *args, **opts):
        qs = Tenant.objects.all()
        if opts.get('tenant'):
            qs = qs.filter(slug=opts['tenant'])
        built, failed = 0, 0
        for tenant in qs:
            path = get_or_build_lead_magnet(tenant, force=opts.get('force'))
            if path:
                built += 1
                self.stdout.write(
                    f"  {tenant.slug}: {design_for(tenant)['key']} -> {path}")
            else:
                failed += 1
                self.stderr.write(f"  {tenant.slug}: FAILED ({storage_path(tenant)})")
        self.stdout.write(self.style.SUCCESS(
            f"Lead magnets: {built} built/cached, {failed} failed."))
