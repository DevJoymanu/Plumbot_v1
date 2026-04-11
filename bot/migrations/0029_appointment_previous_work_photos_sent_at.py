from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('bot', '0028_appointment_retry_count'),
    ]

    operations = [
        migrations.AddField(
            model_name='appointment',
            name='previous_work_photos_sent_at',
            field=models.DateTimeField(
                null=True,
                blank=True,
                help_text='When previous work photos were last sent to this customer',
            ),
        ),
    ]
