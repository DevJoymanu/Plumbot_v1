# Separation of homebase from platform admin: staff users no longer fall back
# to homebase implicitly — every non-superuser needs an explicit
# TenantMembership. Backfill homebase memberships for all existing active
# staff users (superusers excluded — they are platform-level and use View-as).
# Reversible: reverse deletes only the memberships this creates.

from django.db import migrations


def backfill(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    Tenant = apps.get_model('bot', 'Tenant')
    TenantMembership = apps.get_model('bot', 'TenantMembership')
    homebase = Tenant.objects.filter(slug='homebase').first()
    if homebase is None:
        return
    for user in User.objects.filter(is_staff=True, is_superuser=False, is_active=True):
        TenantMembership.objects.get_or_create(
            user=user, tenant=homebase, defaults={'role': 'staff'})


def unbackfill(apps, schema_editor):
    Tenant = apps.get_model('bot', 'Tenant')
    TenantMembership = apps.get_model('bot', 'TenantMembership')
    homebase = Tenant.objects.filter(slug='homebase').first()
    if homebase is not None:
        TenantMembership.objects.filter(tenant=homebase, role='staff').delete()


class Migration(migrations.Migration):
    dependencies = [('bot', '0055_alter_testscenario_name_and_more')]
    operations = [migrations.RunPython(backfill, unbackfill)]
