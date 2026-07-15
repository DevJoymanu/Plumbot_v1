# Phase 0 seed + backfill (docs/MULTI_TENANT_PLAN.md §4 Phase 0.2)
#
# Creates the `homebase` tenant from today's hardcoded business facts (the
# values documented in bot/sales_profiles/homebase.md and bot/faq.py), creates
# its WhatsApp channel from the env vars when present (Railway has them; local
# dev without them just skips the channel row), then backfills tenant_id onto
# every existing business row.
#
# Reversible: reverse is a no-op — reversing 0040 drops the tenant tables and
# columns, which removes everything this migration created (rollback runbook
# Level 2 relies on this chain reversing cleanly).

import os

from django.db import migrations


HOMEBASE_FACTS = dict(
    plumber_name='Takudzwa',
    plumber_contact='+263774819901',
    business_whatsapp='+263776255077',
    location_line="We're in Hatfield, Harare.",
    business_hours={'days': 'Sunday-Friday', 'open': '08:00', 'close': '18:00', 'closed': ['sat']},
    timezone_name='Africa/Johannesburg',
    excluded_areas=['gweru', 'bulawayo', 'mutare', 'masvingo', 'victoria falls', 'hwange', 'beitbridge', 'plumtree'],
    currency='US$',
    packages=[
        {'name': 'Full Bathroom Package', 'price': 800, 'contents': 'shower cubicle + vanity + toilet + chamber + tub'},
        {'name': 'Facebook Package', 'price': 800, 'contents': 'freestanding tub + side chamber'},
    ],
    licensed_claim_enabled=True,  # Homebase's certification claim predates the doc-gating rule
    email_from_name='Takudzwa',
)

# Every table that gained a tenant FK in 0040
BACKFILL_MODELS = [
    'Appointment', 'AppointmentNote', 'ConversationMessage', 'Job',
    'Quotation', 'QuotationTemplate', 'ScheduledFollowup', 'ScheduledReminder',
    'ServiceArea', 'TestScenario', 'WhatsAppInboundEvent',
]


def seed_and_backfill(apps, schema_editor):
    Tenant = apps.get_model('bot', 'Tenant')
    TenantProfile = apps.get_model('bot', 'TenantProfile')
    TenantWhatsAppChannel = apps.get_model('bot', 'TenantWhatsAppChannel')

    tenant, _ = Tenant.objects.get_or_create(
        slug='homebase', defaults={'name': 'Homebase Plumbers'},
    )

    profile_defaults = dict(HOMEBASE_FACTS)
    sales_profile_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'sales_profiles', 'homebase.md',
    )
    if os.path.exists(sales_profile_path):
        with open(sales_profile_path, encoding='utf-8') as fh:
            profile_defaults['sales_profile_md'] = fh.read()
    TenantProfile.objects.get_or_create(tenant=tenant, defaults=profile_defaults)

    phone_number_id = os.environ.get('WHATSAPP_PHONE_NUMBER_ID', '')
    if phone_number_id:
        TenantWhatsAppChannel.objects.get_or_create(
            phone_number_id=phone_number_id,
            defaults=dict(
                tenant=tenant,
                business_account_id=os.environ.get('WHATSAPP_BUSINESS_ACCOUNT_ID', ''),
                access_token=os.environ.get('WHATSAPP_ACCESS_TOKEN', ''),
                verify_token=os.environ.get('WHATSAPP_VERIFY_TOKEN', ''),
            ),
        )

    for model_name in BACKFILL_MODELS:
        model = apps.get_model('bot', model_name)
        model.objects.filter(tenant__isnull=True).update(tenant=tenant)


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0040_tenant_alter_appointment_phone_number_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_and_backfill, migrations.RunPython.noop),
    ]
