from django.conf import settings
from django.db import migrations
from email.utils import parseaddr


def seed_homebase_sender(apps, schema_editor):
    """Pin Homebase's customer-facing sender to the address they already send
    from, in the same deploy that introduces the field.

    Without this there is a window between migrate and someone setting the
    address on the dashboard, during which Homebase's customer mail falls back
    to the platform sender (homebase@notifications.homexmedia.com). That
    delivers fine, but it puts the platform's identity on a customer email that
    has always carried the business's own — a visible regression for the only
    tenant with live customers.

    Only Homebase is seeded: every other tenant must choose its own address, and
    inheriting Homebase's would be exactly the bug this whole change removes.
    """
    Tenant = apps.get_model('bot', 'Tenant')
    TenantProfile = apps.get_model('bot', 'TenantProfile')

    _, current_sender = parseaddr(getattr(settings, 'DEFAULT_FROM_EMAIL', '') or '')
    if not current_sender:
        return

    tenant = Tenant.objects.filter(slug__iexact='homebase').first()
    if tenant is None:
        return

    TenantProfile.objects.filter(
        tenant=tenant, customer_from_email='',
    ).update(customer_from_email=current_sender)


def unseed(apps, schema_editor):
    """Reverse cleanly: clear only what this migration set, so a rollback
    returns Homebase to the platform-sender fallback rather than wiping an
    address someone chose afterwards."""
    Tenant = apps.get_model('bot', 'Tenant')
    TenantProfile = apps.get_model('bot', 'TenantProfile')

    _, current_sender = parseaddr(getattr(settings, 'DEFAULT_FROM_EMAIL', '') or '')
    tenant = Tenant.objects.filter(slug__iexact='homebase').first()
    if tenant is None or not current_sender:
        return

    TenantProfile.objects.filter(
        tenant=tenant, customer_from_email=current_sender,
    ).update(customer_from_email='')


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0067_tenantprofile_customer_from_email'),
    ]

    operations = [
        migrations.RunPython(seed_homebase_sender, unseed),
    ]
