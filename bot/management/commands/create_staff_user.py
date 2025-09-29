from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.db import IntegrityError


class Command(BaseCommand):
    help = 'Create a staff user for the plumbing management system'

    def add_arguments(self, parser):
        parser.add_argument('username', type=str, help='Username for the new staff user')
        parser.add_argument(
            '--email',
            type=str,
            help='Email address for the user',
            default='',
        )
        parser.add_argument(
            '--first-name',
            type=str,
            help='First name of the user',
            default='',
        )
        parser.add_argument(
            '--last-name',
            type=str,
            help='Last name of the user',
            default='',
        )
        parser.add_argument(
            '--superuser',
            action='store_true',
            help='Make this user a superuser (admin)',
        )
        parser.add_argument(
            '--password',
            type=str,
            help='Password for the user (will prompt if not provided)',
        )

    def handle(self, *args, **options):
        username = options['username']
        email = options.get('email', '')
        first_name = options.get('first_name', '')
        last_name = options.get('last_name', '')
        is_superuser = options.get('superuser', False)
        password = options.get('password')

        # Check if user already exists
        if User.objects.filter(username=username).exists():
            self.stdout.write(
                self.style.ERROR(f'User "{username}" already exists!')
            )
            return

        # Get password if not provided
        if not password:
            import getpass
            password = getpass.getpass('Enter password for the new user: ')
            password_confirm = getpass.getpass('Confirm password: ')
            
            if password != password_confirm:
                self.stdout.write(
                    self.style.ERROR('Passwords do not match!')
                )
                return

        try:
            # Create the user
            if is_superuser:
                user = User.objects.create_superuser(
                    username=username,
                    email=email,
                    password=password
                )
            else:
                user = User.objects.create_user(
                    username=username,
                    email=email,
                    password=password
                )
                user.is_staff = True  # Make them staff
                user.save()

            # Set names if provided
            if first_name:
                user.first_name = first_name
            if last_name:
                user.last_name = last_name
            
            user.save()

            user_type = "superuser" if is_superuser else "staff user"
            self.stdout.write(
                self.style.SUCCESS(
                    f'Successfully created {user_type} "{username}"'
                )
            )
            
            self.stdout.write(f'  - Email: {email or "Not provided"}')
            self.stdout.write(f'  - Name: {user.get_full_name() or "Not provided"}')
            self.stdout.write(f'  - Staff: {"Yes" if user.is_staff else "No"}')
            self.stdout.write(f'  - Admin: {"Yes" if user.is_superuser else "No"}')

        except IntegrityError as e:
            self.stdout.write(
                self.style.ERROR(f'Error creating user: {e}')
            )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Unexpected error: {e}')
            )