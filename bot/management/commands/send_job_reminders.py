# management/commands/send_job_reminders.py
# Create directories: management/ and management/commands/
# Run with: python manage.py send_job_reminders

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
import pytz
from bot.models import Appointment  # Replace with your actual app name
from bot.views import twilio_client, TWILIO_WHATSAPP_NUMBER  # Replace with your actual app name


class Command(BaseCommand):
    help = 'Send reminders for upcoming job appointments'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be sent without actually sending',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING('DRY RUN MODE - No messages will be sent')
            )
        
        # Get current time in South Africa timezone
        sa_timezone = pytz.timezone('Africa/Johannesburg')
        now = timezone.now().astimezone(sa_timezone)
        
        # Get upcoming job appointments
        upcoming_jobs = Appointment.objects.filter(
            appointment_type='job_appointment',
            job_status__in=['scheduled'],
            job_scheduled_datetime__isnull=False,
            job_scheduled_datetime__gte=now
        )
        
        reminders_sent = {
            '1_day': 0,
            'morning': 0,
            '2_hours': 0
        }
        
        for job in upcoming_jobs:
            job_time = job.job_scheduled_datetime.astimezone(sa_timezone)
            time_until_job = job_time - now
            hours_until = time_until_job.total_seconds() / 3600
            
            # Check if we need to send any reminders
            reminder_sent = False
            
            # 1 day before reminder (22-26 hours before)
            if 22 <= hours_until <= 26:
                if self.should_send_reminder(job, '1_day'):
                    if self.send_job_reminder(job, '1_day', dry_run):
                        self.mark_reminder_sent(job, '1_day')
                        reminders_sent['1_day'] += 1
                        reminder_sent = True
            
            # Morning of job reminder (6-8 AM on job day)
            elif (job_time.date() == now.date() and 
                  6 <= now.hour <= 8 and 
                  hours_until > 0):
                if self.should_send_reminder(job, 'morning'):
                    if self.send_job_reminder(job, 'morning', dry_run):
                        self.mark_reminder_sent(job, 'morning')
                        reminders_sent['morning'] += 1
                        reminder_sent = True
            
            # 2 hours before reminder
            elif 1.5 <= hours_until <= 2.5:
                if self.should_send_reminder(job, '2_hours'):
                    if self.send_job_reminder(job, '2_hours', dry_run):
                        self.mark_reminder_sent(job, '2_hours')
                        reminders_sent['2_hours'] += 1
                        reminder_sent = True
            
            if reminder_sent:
                self.stdout.write(
                    f"Reminder sent for job {job.id} - {job.customer_name}"
                )
        
        # Summary
        total_sent = sum(reminders_sent.values())
        self.stdout.write(
            self.style.SUCCESS(
                f'Job reminder check completed. Sent {total_sent} reminders:'
            )
        )
        self.stdout.write(f"  - 1-day reminders: {reminders_sent['1_day']}")
        self.stdout.write(f"  - Morning reminders: {reminders_sent['morning']}")
        self.stdout.write(f"  - 2-hour reminders: {reminders_sent['2_hours']}")

    def should_send_reminder(self, job, reminder_type):
        """Check if reminder should be sent"""
        # You might want to add fields to track sent reminders
        # For now, we'll assume reminders haven't been sent
        # In production, add these fields to your model:
        # reminder_1_day_sent = models.BooleanField(default=False)
        # reminder_morning_sent = models.BooleanField(default=False) 
        # reminder_2_hours_sent = models.BooleanField(default=False)
        
        if reminder_type == '1_day':
            return not getattr(job, 'reminder_1_day_sent', False)
        elif reminder_type == 'morning':
            return not getattr(job, 'reminder_morning_sent', False)
        elif reminder_type == '2_hours':
            return not getattr(job, 'reminder_2_hours_sent', False)
        
        return False

    def mark_reminder_sent(self, job, reminder_type):
        """Mark reminder as sent"""
        if reminder_type == '1_day':
            job.reminder_1_day_sent = True
        elif reminder_type == 'morning':
            job.reminder_morning_sent = True
        elif reminder_type == '2_hours':
            job.reminder_2_hours_sent = True
        
        job.save()

    def send_job_reminder(self, job, reminder_type, dry_run=False):
        """Send job appointment reminder"""
        try:
            customer_name = job.customer_name or "Customer"
            job_date = job.job_scheduled_datetime.strftime('%A, %B %d, %Y')
            job_time = job.job_scheduled_datetime.strftime('%I:%M %p')
            plumber_name = job.assigned_plumber.get_full_name() if job.assigned_plumber else "Our team"
            
            if reminder_type == '1_day':
                message = f"""🔧 JOB REMINDER - Tomorrow

Hi {customer_name},

Your plumbing job is scheduled for tomorrow:

📅 Date: {job_date}
🕐 Time: {job_time}
📍 Location: {job.customer_area}
👷 Plumber: {plumber_name}
⏱️ Duration: {job.job_duration_hours} hours

{f"Work: {job.job_description}" if job.job_description else ""}

Please ensure someone is available at the location.

Need to reschedule? Reply to this message.

- Plumbing Team"""

            elif reminder_type == 'morning':
                message = f"""🌅 JOB TODAY

Good morning {customer_name},

Your plumbing job is scheduled for today:

🕐 Time: {job_time}
📍 Location: {job.customer_area}
👷 Plumber: {plumber_name}

{plumber_name} will contact you 30 minutes before arrival.

{f"Work planned: {job.job_description}" if job.job_description else ""}

Questions? Reply to this message.

- Plumbing Team"""

            elif reminder_type == '2_hours':
                message = f"""⏰ JOB IN 2 HOURS

Hi {customer_name},

Your plumbing job starts in approximately 2 hours:

🕐 Time: {job_time}
📍 Location: {job.customer_area}
👷 Plumber: {plumber_name}

{plumber_name} will call you shortly to confirm arrival time.

Please have the work area accessible.

- Plumbing Team"""

            else:
                return False

            if dry_run:
                self.stdout.write(f"Would send {reminder_type} reminder to {job.phone_number}")
                self.stdout.write(f"Message: {message[:100]}...")
                return True
            else:
                # Send actual message
                twilio_client.messages.create(
                    body=message,
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=job.phone_number
                )
                return True

        except Exception as e:
            self.stdout.write(
                self.style.ERROR(
                    f'Failed to send {reminder_type} reminder to {job.phone_number}: {str(e)}'
                )
            )
            return False


# crontab entry example (add to your server's crontab):
# Run every 30 minutes during business hours
# */30 6-18 * * 1-5 /path/to/your/venv/bin/python /path/to/your/project/manage.py send_job_reminders

# Alternative: Django-cron or Celery setup
# Add this to your settings.py if using django-cron:
"""
INSTALLED_APPS = [
    # ... your other apps
    'django_cron',
]

CRON_CLASSES = [
    'your_app.cron.JobReminderCronJob',
]
"""

# your_app/cron.py (if using django-cron)
"""
from django_cron import CronJobBase, Schedule
from django.core.management import call_command

class JobReminderCronJob(CronJobBase):
    RUN_EVERY_MINS = 30  # Run every 30 minutes
    
    schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
    code = 'your_app.job_reminder_cron'
    
    def do(self):
        call_command('send_job_reminders')
"""