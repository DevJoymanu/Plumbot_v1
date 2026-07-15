# Phase 2.5: seed homebase's portfolio from the in-code catalogue
# (bot/portfolio_catalog.PORTFOLIO_ITEMS — stays in code as the seed source
# until prod is verified, then the module reads only from the DB).

from django.db import migrations


def seed(apps, schema_editor):
    from bot.portfolio_catalog import PORTFOLIO_ITEMS
    Tenant = apps.get_model('bot', 'Tenant')
    TenantPortfolioItem = apps.get_model('bot', 'TenantPortfolioItem')
    tenant = Tenant.objects.filter(slug='homebase').first()
    if tenant is None:
        return
    for index, item in enumerate(PORTFOLIO_ITEMS):
        TenantPortfolioItem.objects.get_or_create(
            tenant=tenant, item_id=item['id'],
            defaults=dict(
                filename=item['filename'], title=item['title'],
                price_line=item.get('price', ''),
                description=item.get('description', ''),
                story=item.get('story', ''),
                keywords=list(item.get('keywords', [])),
                sort_order=index,
            ),
        )


def unseed(apps, schema_editor):
    from bot.portfolio_catalog import PORTFOLIO_ITEMS
    Tenant = apps.get_model('bot', 'Tenant')
    TenantPortfolioItem = apps.get_model('bot', 'TenantPortfolioItem')
    tenant = Tenant.objects.filter(slug='homebase').first()
    if tenant is None:
        return
    TenantPortfolioItem.objects.filter(
        tenant=tenant, item_id__in=[i['id'] for i in PORTFOLIO_ITEMS]).delete()


class Migration(migrations.Migration):
    dependencies = [('bot', '0052_tenantportfolioitem_and_more')]
    operations = [migrations.RunPython(seed, unseed)]
