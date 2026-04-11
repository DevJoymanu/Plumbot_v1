from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('bot', '0027_fix_conversationmessage_appointment_fk'),
    ]

    operations = [
        migrations.AddField(
            model_name='appointment',
            name='retry_count',
            field=models.IntegerField(default=0, help_text='Number of retries for current question'),
        ),
    ]
