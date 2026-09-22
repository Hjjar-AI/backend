# backend/apps/core/management/commands/cleanup_database.py

from django.core.management.base import BaseCommand

from apps.core.tasks import cleanup_database


class Command(BaseCommand):
    help = (
        'Run database cleanup: prune old exam sessions, stale '
        'sessions, login attempts, active sessions, and '
        'PrivilegedAction audit rows older than the retention '
        'window (PRIVILEGED_ACTION_RETENTION_DAYS, default 365).'
    )

    def handle(self, *args, **options):
        result = cleanup_database()
        self.stdout.write(self.style.SUCCESS(result))