"""Move existing quotes off the retired 'rejected' status.

0078 changed the choices; this moves the DATA. Without it, any quote already
marked 'rejected' keeps a value that is no longer in STATUS_CHOICES, so
get_status_display() renders the raw string and the quotes list's status filter
cannot match it — the row becomes invisible to every filter while still sitting
in the table.

Reversible on purpose: the backward pass puts them back, so this migration can
be rolled back without inventing a state the old code did not have. It does
mean a quote genuinely marked cold AFTER the rename would go back as
'rejected' — acceptable, because the two mean nearly the same thing to the one
place that reads them, and a stranded value is the worse failure.
"""

from django.db import migrations


def rejected_to_cold(apps, schema_editor):
    Quotation = apps.get_model('bot', 'Quotation')
    Quotation.objects.filter(status='rejected').update(status='cold')


def cold_to_rejected(apps, schema_editor):
    Quotation = apps.get_model('bot', 'Quotation')
    Quotation.objects.filter(status='cold').update(status='rejected')


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0078_quote_branding_and_templates'),
    ]

    operations = [
        migrations.RunPython(rejected_to_cold, cold_to_rejected),
    ]
