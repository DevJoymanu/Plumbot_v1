# Phase 2 slice 1: move the FAQ facts (bot/faq.py::_FACTS) into homebase's
# TenantProfile.faq_facts. After this is verified on production, the in-code
# fallback in faq.py gets deleted and the DB is the single source of truth.
# Reversible: reverse is a no-op (faq.py's transition fallback still answers
# for homebase if the seed is absent).

from django.db import migrations


def seed_faq_facts(apps, schema_editor):
    from bot.tenant_config import HOMEBASE_FAQ_FACTS
    TenantProfile = apps.get_model('bot', 'TenantProfile')
    Tenant = apps.get_model('bot', 'Tenant')
    tenant = Tenant.objects.filter(slug='homebase').first()
    if tenant is None:
        return
    profile = TenantProfile.objects.filter(tenant=tenant).first()
    if profile is None:
        return
    merged = dict(HOMEBASE_FAQ_FACTS)
    merged.update(profile.faq_facts or {})  # never clobber operator edits
    profile.faq_facts = merged
    profile.save(update_fields=['faq_facts'])


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0044_encrypt_channel_tokens'),
    ]

    operations = [
        migrations.RunPython(seed_faq_facts, migrations.RunPython.noop),
    ]
