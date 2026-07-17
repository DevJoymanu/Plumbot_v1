# Backfill lead_source for existing leads: CTWA leads get deterministic ad
# attribution; everyone else gets inference over their FIRST stored customer
# message; no signal = 'direct'. Reversible no-op (the column just empties
# with 0058's reverse).

from django.db import migrations


def backfill(apps, schema_editor):
    import re
    Appointment = apps.get_model('bot', 'Appointment')
    patterns = [
        ('facebook', r"\bfacebook\b|\bfb\s*(page|post|group|ad)?\b"),
        ('instagram', r"\binsta(gram)?\b|\big\s*(page|post)\b"),
        ('google_search', r"\bgoogle\b|\bsearched\b"),
        ('whatsapp_status', r"\b(whatsapp\s+)?status\b"),
        ('referral', r"\breferr?ed\b|\brecommend\w*\b|\bword of mouth\b"),
        ('flyer', r"\bflyer\b|\bposter\b|\bbanner\b"),
    ]
    for apt in Appointment.objects.filter(lead_source=''):
        if apt.ctwa_referral:
            url = str((apt.ctwa_referral or {}).get('source_url') or '').lower()
            apt.lead_source = 'instagram_ad' if 'instagram' in url else 'facebook_ad'
        else:
            first = next((m.get('content', '') for m in (apt.conversation_history or [])
                          if isinstance(m, dict) and m.get('role') == 'user'), '')
            text = (first or '').lower()
            apt.lead_source = next(
                (s for s, p in patterns if re.search(p, text)), 'direct')
        apt.save(update_fields=['lead_source'])


class Migration(migrations.Migration):
    dependencies = [('bot', '0058_appointment_lead_source')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
