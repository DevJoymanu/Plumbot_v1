# bot/migrations/0XXX_add_followup_tracking_fields.py
# Run: python manage.py makemigrations
# Then: python manage.py migrate

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0016_alter_appointment_options_and_more'),  # Replace with your latest migration
    ]

    operations = [
        migrations.AddField(
            model_name='appointment',
            name='is_automatic_followup',
            field=models.BooleanField(
                default=False,
                help_text='True if last follow-up was automatic, False if manual'
            ),
        ),
        migrations.AddField(
            model_name='appointment',
            name='automatic_followup_count',
            field=models.IntegerField(
                default=0,
                help_text='Number of automatic follow-ups sent'
            ),
        ),
        migrations.AddField(
            model_name='appointment',
            name='manual_followup_count',
            field=models.IntegerField(
                default=0,
                help_text='Number of manual follow-ups sent by staff'
            ),
        ),
        migrations.AddField(
            model_name='appointment',
            name='last_automatic_followup_sent',
            field=models.DateTimeField(
                null=True,
                blank=True,
                help_text='Last time automatic follow-up was sent'
            ),
        ),
        migrations.AddField(
            model_name='appointment',
            name='last_manual_followup_sent',
            field=models.DateTimeField(
                null=True,
                blank=True,
                help_text='Last time manual follow-up was sent by staff'
            ),
        ),
        migrations.AddField(
            model_name='appointment',
            name='manual_followup_paused',
            field=models.BooleanField(
                default=False,
                help_text='Pause automatic follow-ups when staff is manually engaging'
            ),
        ),
        migrations.AddField(
            model_name='appointment',
            name='manual_followup_paused_until',
            field=models.DateTimeField(
                null=True,
                blank=True,
                help_text='Resume automatic follow-ups after this date'
            ),
        ),
    ]