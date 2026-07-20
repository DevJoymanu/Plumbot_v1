# Kitchens got their own gallery bucket. 0062 had to park homebase's two
# kitchen-renovation photos in 'general' because GALLERY_CATEGORIES had no
# kitchen key — even though renovation/kitchen is a priced job (US$600) and
# the annotator's PORTFOLIO_LIBRARY was missing every renovation, so no
# tenant could tag a kitchen photo at all. Both are fixed in code; this
# re-tags the rows 0062 already wrote.

from django.db import migrations


def retag(apps, schema_editor):
    from bot.portfolio_catalog import PORTFOLIO_ITEMS
    Tenant = apps.get_model('bot', 'Tenant')
    TenantPortfolioItem = apps.get_model('bot', 'TenantPortfolioItem')
    tenant = Tenant.objects.filter(slug='homebase').first()
    if tenant is None:
        return
    wanted = {i['id']: i.get('category', 'general') for i in PORTFOLIO_ITEMS}
    for row in TenantPortfolioItem.objects.filter(tenant=tenant):
        category = wanted.get(row.item_id)
        # Only correct rows still carrying 0062's tag — an owner who has since
        # re-categorised a photo by hand keeps their choice.
        if category is None or row.keywords == [category] or len(row.keywords or []) != 1:
            continue
        if row.keywords[0] not in ('general', 'bathroom install'):
            continue
        row.keywords = [category]
        row.save(update_fields=['keywords'])


def untag(apps, schema_editor):
    Tenant = apps.get_model('bot', 'Tenant')
    TenantPortfolioItem = apps.get_model('bot', 'TenantPortfolioItem')
    tenant = Tenant.objects.filter(slug='homebase').first()
    if tenant is None:
        return
    TenantPortfolioItem.objects.filter(
        tenant=tenant, keywords=['kitchen']).update(keywords=['general'])


class Migration(migrations.Migration):
    dependencies = [('bot', '0062_portfolio_match_terms')]
    operations = [migrations.RunPython(retag, untag)]
