from django.db import migrations, models


class Migration(migrations.Migration):
    """A deposit the business can adjust, and somewhere to keep the number a
    standalone quote is sent to.

    Both default to the value every existing quote already behaves as: 0% is
    no deposit row at all, and a blank client_phone falls back to the lead's
    own WhatsApp number exactly as before.
    """

    dependencies = [
        ('bot', '0085_phonequoterequest'),
    ]

    operations = [
        migrations.AddField(
            model_name='quotation',
            name='deposit_percent',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AddField(
            model_name='quotation',
            name='client_phone',
            field=models.CharField(blank=True, max_length=50),
        ),
    ]
