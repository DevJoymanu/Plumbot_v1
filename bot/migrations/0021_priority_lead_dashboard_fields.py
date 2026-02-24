from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bot", "0020_align_leadinteraction_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="last_priority_alert_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="last_priority_alert_summary",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="manual_followup_done",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="appointment",
            name="manual_followup_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
