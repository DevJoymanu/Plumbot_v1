# Create this file: your_app/management/commands/send_reminders.py

f#rom django.core.management.base import BaseCommand
#from django.utils import timezone
#from bot.views import check_and_send_reminders  # Import from your views
from django.core.management.base import BaseCommand
from bot.views import run_reminder_scheduler

class Command(BaseCommand):
    help = 'Check and send appointment reminders'
    
    def handle(self, *args, **options):
        self.stdout.write('Starting reminder check...')
        success = run_reminder_scheduler()
        
        if success:
            self.stdout.write(
                self.style.SUCCESS('Reminder check completed successfully')
            )
        else:
            self.stdout.write(
                self.style.ERROR('Reminder check failed')
            )
