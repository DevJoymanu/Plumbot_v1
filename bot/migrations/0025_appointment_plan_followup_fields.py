from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Adds two fields to Appointment needed by the plan follow-up system:

      plan_followup_attempts    — how many plan-upload nudges we've sent
      plan_followup_not_before  — earliest datetime the next nudge can fire
                                  (derived from the customer's promise, e.g. "tomorrow")
    """

    dependencies = [
        # Replace with your actual last migration name
        ('bot', '0024_appointment_sent_pricing_intents'),
    ]

    operations = [
        migrations.AddField(
            model_name='appointment',
            name='plan_followup_attempts',
            field=models.IntegerField(
                default=0,
                help_text='Number of plan-upload follow-up messages sent',
            ),
        ),
        migrations.AddField(
            model_name='appointment',
            name='plan_followup_not_before',
            field=models.DateTimeField(
                null=True,
                blank=True,
                db_index=True,
                help_text=(
                    'Do not send a plan follow-up before this UTC datetime. '
                    'Derived from the customer\'s promise ("I\'ll send tomorrow", etc.).'
                ),
            ),
        ),
    ]