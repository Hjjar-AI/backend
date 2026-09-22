# tests/master_exams/test_sweeper_limit.py
"""
`MasterExamSweeper.sweep_expired(limit=N)` must process at most N
attempts per call. The default is 200; the cron-friendly management
command runs it on a schedule and relies on the bound to cap the
work done in a single run.

A regression to an unbounded scan would be invisible until it hit
a table large enough to matter.
"""
from datetime import timedelta

from django.utils import timezone

from apps.master_exams.models import (
    MasterExam, MasterExamQuestion, MasterExamAttempt,
)
from apps.master_exams.services import MasterExamAttemptService, MasterExamSweeper
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


def _exam(author, question):
    now = timezone.now()
    exam = MasterExam.objects.create(
        name='Sweep Test',
        primary_attending=author,
        opens_at=now - timedelta(hours=2),
        closes_at=now - timedelta(hours=1),
        duration_minutes=60,
        stored_status='published',
        audience_all_doctors=True,
    )
    MasterExamQuestion.objects.create(
        master_exam=exam, question=question, order=1,
    )
    return exam


class SweepExpiredLimitTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.q = make_question(owner=self.author)
        self.exam = _exam(self.author, self.q)

        # Five students, each with an expired attempt.
        self.students = []
        for i in range(5):
            student = make_user(f'student_{i}')
            attempt = MasterExamAttemptService.start(student, self.exam)
            attempt.deadline_at = timezone.now() - timedelta(hours=1)
            attempt.save()
            self.students.append(student)

    def test_limit_bounds_single_run(self):
        count = MasterExamSweeper.sweep_expired(limit=2)
        self.assertEqual(count, 2)
        self.assertEqual(
            MasterExamAttempt.objects.filter(is_complete=True).count(), 2,
        )

    def test_subsequent_run_finishes_remaining(self):
        MasterExamSweeper.sweep_expired(limit=2)
        count = MasterExamSweeper.sweep_expired(limit=2)
        self.assertEqual(count, 2)
        count = MasterExamSweeper.sweep_expired(limit=2)
        self.assertEqual(count, 1)
        self.assertEqual(
            MasterExamAttempt.objects.filter(is_complete=False).count(), 0,
        )

    def test_default_limit_processes_everything_below_bound(self):
        count = MasterExamSweeper.sweep_expired()
        self.assertEqual(count, 5)