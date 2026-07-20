from django.apps import AppConfig


class BotConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'bot'

    def ready(self):
        from django.conf import settings
        if getattr(settings, 'TESTING', False):
            # Test databases are built straight from models (bot migrations are
            # skipped — see settings.MIGRATION_MODULES), so the homebase seed
            # that migration 0041 gives real databases never runs there. The
            # tenant FK is non-null with a default that resolves the `homebase`
            # slug, so seed it right after the test schema is created —
            # otherwise every untagged Appointment.objects.create() in the
            # suites fails the NOT NULL constraint.
            from django.db.models.signals import post_migrate
            post_migrate.connect(_seed_test_tenant, sender=self)


def _seed_test_tenant(sender, **kwargs):
    # Mirror migrations 0041 + 0045 + 0048: tenant + fully populated profile +
    # price sheet, so tests exercise the same config the production homebase
    # tenant has.
    from .models import Tenant, TenantPriceItem, TenantProfile
    from .tenant_config import (
        HOMEBASE_FAQ_FACTS, HOMEBASE_PRICE_ITEMS, HOMEBASE_PROFILE_FIELDS,
    )
    tenant, _ = Tenant.objects.get_or_create(
        slug='homebase', defaults={'name': 'Homebase Plumbers'})
    TenantProfile.objects.get_or_create(
        tenant=tenant,
        defaults=dict(HOMEBASE_PROFILE_FIELDS, faq_facts=dict(HOMEBASE_FAQ_FACTS)),
    )
    for row in HOMEBASE_PRICE_ITEMS:
        data = dict(row)
        family = data.pop('family')
        variant = data.pop('variant', '')
        TenantPriceItem.objects.get_or_create(
            tenant=tenant, family=family, variant=variant, defaults=data,
        )
    # Mirror migration 0053: homebase portfolio rows.
    from .models import TenantPortfolioItem
    from .portfolio_catalog import PORTFOLIO_ITEMS
    for index, item in enumerate(PORTFOLIO_ITEMS):
        TenantPortfolioItem.objects.get_or_create(
            tenant=tenant, item_id=item['id'],
            defaults=dict(
                filename=item['filename'], title=item['title'],
                price_line=item.get('price', ''),
                description=item.get('description', ''),
                story=item.get('story', ''),
                keywords=[item.get('category', 'general')],
                match_terms=list(item.get('keywords', [])),
                sort_order=index,
            ),
        )
