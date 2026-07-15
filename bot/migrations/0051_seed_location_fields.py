# Phase 2.4: seed the split location fields for homebase (used to compose
# location sentences in copy/prompts; location_line stays the FAQ sentence).

from django.db import migrations


def seed(apps, schema_editor):
    TenantProfile = apps.get_model('bot', 'TenantProfile')
    Tenant = apps.get_model('bot', 'Tenant')
    tenant = Tenant.objects.filter(slug='homebase').first()
    if tenant is None:
        return
    TenantProfile.objects.filter(tenant=tenant, location_area='').update(
        location_area='Hatfield', location_city='Harare')


class Migration(migrations.Migration):
    dependencies = [('bot', '0050_tenantprofile_location_area_and_more')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
