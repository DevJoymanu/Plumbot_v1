from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bot", "0021_priority_lead_dashboard_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="last_unconfirmed_summary_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="last_unconfirmed_summary_text",
            field=models.TextField(blank=True),
        ),
        migrations.DeleteModel(
            name="LeadInteraction",
        ),
    ]
