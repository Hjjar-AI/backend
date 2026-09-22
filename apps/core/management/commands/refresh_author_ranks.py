# backend/apps/core/management/commands/refresh_author_ranks.py

from django.core.management.base import BaseCommand

from apps.questions.services import AuthorReputationService


class Command(BaseCommand):
    help = (
        'Recompute User.trust_score and User.questions_count for every '
        'non-admin contributor. Idempotent — writes only on change.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--quiet', '-q',
            action='store_true',
            help='Suppress the summary line on success (for cron).',
        )

    def handle(self, *args, **options):
        result = AuthorReputationService.refresh_all()
        if not options['quiet']:
            self.stdout.write(self.style.SUCCESS(
                f"Author reputation refresh complete: "
                f"scanned={result['scanned']}, "
                f"updated={result['updated']}."
            ))