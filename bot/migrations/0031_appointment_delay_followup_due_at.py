from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0030_appointment_is_delayed'),
    ]

    operations = [
        migrations.AddField(
            model_name='appointment',
            name='delay_followup_due_at',
            field=models.DateTimeField(
                null=True,
                blank=True,
                db_index=True,
                help_text=(
                    '14 days after delay_signal_detected_at. '
                    'The plumber should manually follow up by this date.'
                ),
            ),
        ),
    ]