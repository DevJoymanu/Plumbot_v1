"""A quotation TEMPLATE can carry sections, like the quote sheet it fills in.

`section` and `quantity_text` mirror the same two fields on QuotationItem, so a
template built on the sectioned builder arrives on a quote as its own numbered
sections with the trade's own quantity wording ("19 length"), rather than as
one flat block named after the template.

Both are blank by default, so every template that exists today is unchanged:
one untitled group, which is exactly what a flat template has always been.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0086_quotation_deposit_and_client_phone'),
    ]

    operations = [
        migrations.AddField(
            model_name='quotationtemplateitem',
            name='section',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
        migrations.AddField(
            model_name='quotationtemplateitem',
            name='quantity_text',
            field=models.CharField(blank=True, default='', max_length=40),
        ),
        migrations.AlterModelOptions(
            name='quotationtemplateitem',
            options={'ordering': ['sort_order', 'id']},
        ),
    ]
