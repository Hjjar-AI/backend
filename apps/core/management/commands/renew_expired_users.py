from django.core.management.base import BaseCommand
from apps.core.tasks import renew_expired_users

class Command(BaseCommand):
    help = 'Renew expired non-admin users who have auto_renew_days > 0'

    def handle(self, *args, **options):
        result = renew_expired_users()
        self.stdout.write(self.style.SUCCESS(result))