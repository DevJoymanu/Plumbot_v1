from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

class Command(BaseCommand):
    help = 'Creates a superuser if none exists'

    def handle(self, *args, **options):
        User = get_user_model()
        if not User.objects.filter(username='admin').exists():
            User.objects.create_superuser(
                username='admin',
                email='admin@plumbingcrm.com',
                password='Admin123!Change'
            )
            self.stdout.write(self.style.SUCCESS('✓ Superuser created successfully!'))
        else:
            self.stdout.write(self.style.WARNING('Superuser already exists'))