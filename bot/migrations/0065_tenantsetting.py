"""PlatformSetting (global) -> TenantSetting (per tenant).

The switches shipped global one day earlier; every tenant now owns its own
copy. Any global row already set in production is fanned out to EVERY tenant
so the admin's existing choice is preserved rather than silently reset to the
default.
"""

import django.db.models.deletion
from django.db import migrations, models

import bot.models


def fan_out_global_settings(apps, schema_editor):
    PlatformSetting = apps.get_model('bot', 'PlatformSetting')
    TenantSetting = apps.get_model('bot', 'TenantSetting')
    Tenant = apps.get_model('bot', 'Tenant')
    tenant_ids = list(Tenant.objects.values_list('pk', flat=True))
    for setting in PlatformSetting.objects.all():
        for tenant_id in tenant_ids:
            TenantSetting.objects.update_or_create(
                tenant_id=tenant_id, key=setting.key,
                defaults={'value': setting.value})


def noop(apps, schema_editor):
    """Reverse: the global table is recreated empty — every tenant row stays
    put, so nothing is lost, the switches just read as default again."""


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0064_platformsetting'),
    ]

    operations = [
        migrations.CreateModel(
            name='TenantSetting',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=64)),
                ('value', models.JSONField(default=dict)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('tenant', models.ForeignKey(
                    blank=True, default=bot.models.get_default_tenant_id,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='settings', to='bot.tenant')),
            ],
            options={
                'ordering': ['tenant_id', 'key'],
            },
        ),
        migrations.AddConstraint(
            model_name='tenantsetting',
            constraint=models.UniqueConstraint(
                fields=('tenant', 'key'), name='uniq_setting_per_tenant'),
        ),
        migrations.RunPython(fan_out_global_settings, noop),
        migrations.DeleteModel(name='PlatformSetting'),
    ]
