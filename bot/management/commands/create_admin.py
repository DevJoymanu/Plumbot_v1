from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

class Command(BaseCommand):
    def handle(self, *args, **options):
        User = get_user_model()
        if not User.objects.filter(username='admin').exists():
            User.objects.create_superuser(
                username='Homebase',
                email='admin@homebase.com',
                password='TempPassword123!'  # Change this after first login
            )
            self.stdout.write('Superuser created!')