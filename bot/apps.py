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
    # Mirror migrations 0041 + 0045: tenant + fully populated profile, so
    # tests exercise the same config the production homebase tenant has.
    from .models import Tenant, TenantProfile
    from .tenant_config import HOMEBASE_FAQ_FACTS, HOMEBASE_PROFILE_FIELDS
    tenant, _ = Tenant.objects.get_or_create(
        slug='homebase', defaults={'name': 'Homebase Plumbers'})
    TenantProfile.objects.get_or_create(
        tenant=tenant,
        defaults=dict(HOMEBASE_PROFILE_FIELDS, faq_facts=dict(HOMEBASE_FAQ_FACTS)),
    )
