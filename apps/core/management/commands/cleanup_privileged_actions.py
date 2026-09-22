# backend/apps/core/management/commands/cleanup_privileged_actions.py
"""
Standalone audit-log sweep.

Wraps `tasks.cleanup_privileged_actions()` so the retention sweep can
be scheduled independently of the rest of `cleanup_database`. Use
this when you want a short-frequency audit sweep (say, weekly) and a
long-frequency aggregate cleanup (say, nightly), or when you want to
run the audit sweep manually after a large batch operation without
also touching session/login tables.

Usage:
    python manage.py cleanup_privileged_actions
    python manage.py cleanup_privileged_actions --days 90
    python manage.py cleanup_privileged_actions --dry-run
    python manage.py cleanup_privileged_actions --quiet
"""

from django.core.management.base import BaseCommand

from apps.core.tasks import cleanup_privileged_actions, privileged_action_retention


class Command(BaseCommand):
    help = (
        'Delete PrivilegedAction rows older than the retention '
        'window (default: PRIVILEGED_ACTION_RETENTION_DAYS, or 365).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=None,
            help=(
                'Override the retention window for this run only. '
                'Takes precedence over the setting. Minimum 1.'
            ),
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be deleted without deleting.',
        )
        parser.add_argument(
            '--quiet', '-q', action='store_true',
            help='Suppress the summary line on success (for cron).',
        )

    def handle(self, *args, **options):
        from apps.core.models import PrivilegedAction

        days = options['days']
        dry_run = options['dry_run']
        quiet = options['quiet']

        if dry_run:
            # Use the same effective retention policy as deletion.
            effective_days, cutoff = privileged_action_retention(days)
            would_delete = PrivilegedAction.objects.filter(
                timestamp__lt=cutoff,
            ).count()
            if not quiet:
                self.stdout.write(self.style.WARNING(
                    f'[dry-run] would delete {would_delete} PrivilegedAction '
                    f'row(s) older than {effective_days} day(s) '
                    f'(cutoff: {cutoff.isoformat()}).'
                ))
            return

        deleted = cleanup_privileged_actions(retention_days=days)

        if quiet:
            return

        # Report the effective window so the operator can confirm the
        # setting was picked up as intended.
        effective_days, _ = privileged_action_retention(days)

        self.stdout.write(self.style.SUCCESS(
            f'Deleted {deleted} PrivilegedAction row(s) older than '
            f'{effective_days} day(s).'
        ))
