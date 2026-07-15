# Phase 1.2: encrypt existing TenantWhatsAppChannel access tokens at rest.
# New/edited rows encrypt in save(); this catches rows written before the
# hook existed (the homebase seed). Idempotent — encrypt_secret() passes
# already-encrypted values through. Reverse is a no-op: decrypt_secret()
# transparently reads both plaintext and encrypted values, so older code
# levels never break on encrypted rows they can't decrypt only if they
# predate 0040 entirely — and reversing to there drops the table anyway.

from django.db import migrations


def encrypt_tokens(apps, schema_editor):
    from bot.services.secrets import encrypt_secret
    TenantWhatsAppChannel = apps.get_model('bot', 'TenantWhatsAppChannel')
    for channel in TenantWhatsAppChannel.objects.exclude(access_token=''):
        encrypted = encrypt_secret(channel.access_token)
        if encrypted != channel.access_token:
            channel.access_token = encrypted
            channel.save(update_fields=['access_token'])


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0043_alter_appointment_tenant_and_more'),
    ]

    operations = [
        migrations.RunPython(encrypt_tokens, migrations.RunPython.noop),
    ]
