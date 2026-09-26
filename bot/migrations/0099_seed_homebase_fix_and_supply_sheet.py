"""Switch Homebase onto its own "fix and supply" quote sheet and seed the
letterhead facts that sheet prints.

WHAT: sets letterhead.layout = "fix_and_supply" on the `homebase` tenant, plus
the facts off Homebase's own paper quote (the registered company name, the two
cell numbers, email, website, street address, bank details, tagline and the
HOMEBASE watermark). From then on every Homebase quote screen, the template
builder and the PDF draw that sheet.

WHY a migration and not a code default: the layout is tenant DATA, never a
slug check in code (the same rule migration 0070 follows for Barmak), so no
other tenant can ever pick this sheet or these facts up. They live on
Homebase's profile row and nowhere else.

HOW: scoped to slug `homebase`, a no-op where that tenant is absent, and it
only FILLS BLANKS - a value already set on the Profile page is kept, including
a layout someone has already chosen. location_line is deliberately NOT
touched: it is also the sentence the assistant says ("We're in Hatfield,
Harare."), so the street address goes in `sheet_address`, which only the sheet
reads. No bank name is seeded because the paper names only the branch.
"""
from django.db import migrations

SLUG = 'homebase'

LETTERHEAD = {
    'layout': 'fix_and_supply',
    'company_name': 'HOMEBASE CONSTRUCTION [PVT]LTD',
    'watermark': 'HOMEBASE',
    'sheet_address': '150 Northway | Seke rd, Hatfield, Harare',
    'phones': ['0774 819 901', '0772 254 823'],
    'public_email': 'info@homebaseplumbers.co.zw',
    'website': 'www.homebaseplumbers.co.zw',
    'tagline': 'Quality Is Our Qualification',
    'bank': {
        'account_name': 'HOMEBASE CONSTRUCTION',
        'branch': 'FBC CENTER',
        'account_number': '4482103480101',
    },
}


def seed(apps, schema_editor):
    Tenant = apps.get_model('bot', 'Tenant')
    TenantProfile = apps.get_model('bot', 'TenantProfile')

    tenant = Tenant.objects.filter(slug=SLUG).first()
    if tenant is None:
        return

    profile, _ = TenantProfile.objects.get_or_create(tenant=tenant)
    existing = profile.letterhead if isinstance(profile.letterhead, dict) else {}

    merged = dict(LETTERHEAD)
    merged.update({k: v for k, v in existing.items() if v not in (None, '', [], {})})
    # The bank block is merged field by field: a Profile save writes every bank
    # key (blank ones as ''), so a dict of blanks must not hide the seed.
    bank = dict(LETTERHEAD['bank'])
    old_bank = existing.get('bank') if isinstance(existing.get('bank'), dict) else {}
    bank.update({k: v for k, v in old_bank.items() if v})
    merged['bank'] = bank

    profile.letterhead = merged
    profile.save(update_fields=['letterhead'])


def unseed(apps, schema_editor):
    """Back to the flat layout; the facts stay so a re-apply need not retype
    them."""
    TenantProfile = apps.get_model('bot', 'TenantProfile')
    profile = TenantProfile.objects.filter(tenant__slug=SLUG).first()
    if profile is None or not isinstance(profile.letterhead, dict):
        return
    if profile.letterhead.get('layout') == 'fix_and_supply':
        profile.letterhead.pop('layout', None)
        profile.save(update_fields=['letterhead'])


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0098_billing_ecocash_contact'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
