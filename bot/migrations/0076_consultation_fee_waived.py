from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0075_reminder_2_days_sent'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenantprofile',
            name='consultation_fee_waived_on_job',
            field=models.BooleanField(default=False),
        ),
    ]
