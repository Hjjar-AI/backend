# backend/apps/core/management/commands/sweep_master_exams.py

from django.core.management.base import BaseCommand

from apps.master_exams.services.master_exam_sweeper import MasterExamSweeper

class Command(BaseCommand):
    help = 'Force-finish master exam attempts whose deadline has passed.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=200,
            help='Maximum number of attempts to finish in one run.',
        )

    def handle(self, *args, **options):
        finished = MasterExamSweeper.sweep_expired(limit=options['limit'])
        self.stdout.write(self.style.SUCCESS(
            f'Sweep complete: {finished} attempt(s) finished.'
        ))