# Phase 2.3d: the tub measurement blocks (formerly ResponseMixin
# ._TUB_SIZE_BLOCKS) move into the tub rows' `sizes`. Updates homebase's
# existing tub rows and creates the corner-tub row (sizes only — corner is
# priced as a built-in, owner rule). Values come from the canonical
# HOMEBASE_PRICE_ITEMS so migration, test hook, and code agree.
# Reverse: no-op (extra sizes data is harmless to older code levels).

from django.db import migrations


def seed_tub_sizes(apps, schema_editor):
    from bot.tenant_config import HOMEBASE_PRICE_ITEMS
    Tenant = apps.get_model('bot', 'Tenant')
    TenantPriceItem = apps.get_model('bot', 'TenantPriceItem')
    tenant = Tenant.objects.filter(slug='homebase').first()
    if tenant is None:
        return
    for row in HOMEBASE_PRICE_ITEMS:
        if row['family'] != 'tub':
            continue
        data = dict(row)
        family = data.pop('family')
        variant = data.pop('variant', '')
        item, created = TenantPriceItem.objects.get_or_create(
            tenant=tenant, family=family, variant=variant, defaults=data,
        )
        if not created and row.get('sizes') and item.sizes != row['sizes']:
            item.sizes = row['sizes']
            item.save(update_fields=['sizes'])


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0048_seed_homebase_price_items'),
    ]

    operations = [
        migrations.RunPython(seed_tub_sizes, migrations.RunPython.noop),
    ]
