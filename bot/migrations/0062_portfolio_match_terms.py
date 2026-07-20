# Homebase's gallery categories were never the app's gallery categories.
# Migration 0053 seeded TenantPortfolioItem.keywords from the in-code
# catalogue, where `keywords` means "words a customer might use to point at
# this photo" ('navy', 'clawfoot', 'gold tap'). Everywhere else in the app
# `keywords` means the gallery category tag — one of GALLERY_CATEGORIES'
# closed set — so homebase's portal Gallery grouped its photos into eleven
# one-item buckets titled after synonyms instead of Bathroom installs /
# General like every other tenant's.
#
# Split the two: match terms move to the new `match_terms` field (the bot's
# specific-photo matching keeps working untouched), and `keywords` becomes the
# category from the catalogue's `category` key.

from django.db import migrations, models


def split_terms(apps, schema_editor):
    from bot.portfolio_catalog import PORTFOLIO_ITEMS
    Tenant = apps.get_model('bot', 'Tenant')
    TenantPortfolioItem = apps.get_model('bot', 'TenantPortfolioItem')
    tenant = Tenant.objects.filter(slug='homebase').first()
    if tenant is None:
        return
    seeded = {i['id']: i for i in PORTFOLIO_ITEMS}
    for row in TenantPortfolioItem.objects.filter(tenant=tenant):
        item = seeded.get(row.item_id)
        if item is None or row.match_terms:
            continue           # not a seeded row, or already split — leave it
        category = item.get('category', 'general')
        # A resync (media_library.resync_portfolio_prices) may already have
        # replaced this row's synonyms with category tags — then the row has
        # no match terms left to move and the catalogue is the only source.
        row.match_terms = ([t for t in (row.keywords or []) if t != category]
                           or list(item.get('keywords', [])))
        row.keywords = [category]
        row.save(update_fields=['match_terms', 'keywords'])


def rejoin_terms(apps, schema_editor):
    Tenant = apps.get_model('bot', 'Tenant')
    TenantPortfolioItem = apps.get_model('bot', 'TenantPortfolioItem')
    tenant = Tenant.objects.filter(slug='homebase').first()
    if tenant is None:
        return
    for row in TenantPortfolioItem.objects.filter(tenant=tenant).exclude(match_terms=[]):
        row.keywords = list(row.match_terms)
        row.save(update_fields=['keywords'])


class Migration(migrations.Migration):
    dependencies = [('bot', '0061_tenantportfolioitem_price_refs')]
    operations = [
        migrations.AddField(
            model_name='tenantportfolioitem',
            name='match_terms',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(split_terms, rejoin_terms),
    ]
