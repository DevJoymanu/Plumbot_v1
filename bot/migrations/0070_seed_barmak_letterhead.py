"""Seed Barmak Plumbing's quote letterhead and switch them to the sectioned
quote layout.

Scoped to the `barmak-plumbing` tenant by slug and a no-op when that tenant is
absent (fresh databases, other deployments). Nothing here is a platform
default: these are Barmak's own business facts, and no other tenant's quote can
pick them up because they live on Barmak's profile row.

Only fills blanks — if someone has already edited the letterhead on the Profile
page, this leaves it alone.
"""
from django.db import migrations

SLUG = 'barmak-plumbing'

LETTERHEAD = {
    'layout': 'sectioned',
    'trading_name': 'ROYAL HARDWARE',
    'phones': [
        '+263 77 387 1503',
        '+263 77 324 0167',
        '+263 718 744 685',
        '+263 713 152 080',
    ],
    'public_email': 'info@barmakplumbing.co.zw',
    'website': 'www.barmakplumbing.co.zw',
    'services_blurb': (
        'For all: supply & new installation water & sewer reticulation, hot & cold '
        'water & drain laying, all types of geyser, storage (jojo) tanks & tank '
        'stands, gutters, flushing, toilet, tubs, wash hand basin, sink, shower & '
        'all type mixer, irrigation, all submersible, well, booster pump, bathroom '
        'accessories.'
    ),
    'maintenance_blurb': 'Maintenance: water leaks, no water, low pressure & blockages.',
    'tagline': 'Quality is our mission',
    'signatory': 'Director K. Marange',
    'bank': {
        'account_name': 'Barmak Plumbing Private Limited',
        'bank_name': 'CABS',
        'branch': 'Park street',
        'account_number': '1154714543',
    },
    'terms': [
        'deposit 75%',
        'Balance to be paid on completion of 1st stage',
    ],
    'default_vat_percent': 0,
}

ADDRESS = '20398 Budiriro 5B Cabs Harare'


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
    profile.letterhead = merged

    fields = ['letterhead']
    if not (profile.location_line or '').strip():
        profile.location_line = ADDRESS
        fields.append('location_line')

    profile.save(update_fields=fields)


def unseed(apps, schema_editor):
    """Drop back to the flat layout; leave the facts in place so a re-apply
    does not have to retype them."""
    TenantProfile = apps.get_model('bot', 'TenantProfile')
    profile = TenantProfile.objects.filter(tenant__slug=SLUG).first()
    if profile is None or not isinstance(profile.letterhead, dict):
        return
    profile.letterhead.pop('layout', None)
    profile.save(update_fields=['letterhead'])


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0069_alter_quotationitem_options_quotation_discount_and_more'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
