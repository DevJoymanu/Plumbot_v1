from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0090_appointment_job_end_datetime'),
    ]

    operations = [
        migrations.AddField(
            model_name='sitevisitreport',
            name='sequence_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
