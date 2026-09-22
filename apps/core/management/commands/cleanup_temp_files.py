from django.core.management.base import BaseCommand
from apps.core.tasks import cleanup_temp_files

class Command(BaseCommand):
    help = 'Delete old temporary import/export/backup files from upload/export/backup folders'

    def handle(self, *args, **options):
        result = cleanup_temp_files()
        self.stdout.write(self.style.SUCCESS(result))