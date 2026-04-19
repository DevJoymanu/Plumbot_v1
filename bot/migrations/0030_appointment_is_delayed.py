from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0029_appointment_previous_work_photos_sent_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='appointment',
            name='is_delayed',
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text='True when customer has signalled they are not ready yet (delay signal detected)',
            ),
        ),
        migrations.AddField(
            model_name='appointment',
            name='delay_signal_detected_at',
            field=models.DateTimeField(
                null=True,
                blank=True,
                help_text='Timestamp when the delay signal was first detected',
            ),
        ),
    ]