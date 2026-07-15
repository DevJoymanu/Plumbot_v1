# Phase 2.3: seed homebase's price sheet into TenantPriceItem — the numbers
# from bot/sales_profiles/homebase.md / the response_mixin tables, via the
# canonical HOMEBASE_PRICE_ITEMS constant (also used by the test-DB hook).
# Idempotent (get_or_create) and reversible (reverse deletes only rows that
# exactly match a seed key for homebase).

from django.db import migrations


def seed_prices(apps, schema_editor):
    from bot.tenant_config import HOMEBASE_PRICE_ITEMS
    Tenant = apps.get_model('bot', 'Tenant')
    TenantPriceItem = apps.get_model('bot', 'TenantPriceItem')
    tenant = Tenant.objects.filter(slug='homebase').first()
    if tenant is None:
        return
    for row in HOMEBASE_PRICE_ITEMS:
        data = dict(row)
        family = data.pop('family')
        variant = data.pop('variant', '')
        TenantPriceItem.objects.get_or_create(
            tenant=tenant, family=family, variant=variant, defaults=data,
        )


def unseed_prices(apps, schema_editor):
    from bot.tenant_config import HOMEBASE_PRICE_ITEMS
    Tenant = apps.get_model('bot', 'Tenant')
    TenantPriceItem = apps.get_model('bot', 'TenantPriceItem')
    tenant = Tenant.objects.filter(slug='homebase').first()
    if tenant is None:
        return
    for row in HOMEBASE_PRICE_ITEMS:
        TenantPriceItem.objects.filter(
            tenant=tenant, family=row['family'], variant=row.get('variant', ''),
        ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0047_tenantpriceitem_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_prices, unseed_prices),
    ]
