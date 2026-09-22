# tests/edge_cases/test_concurrency.py
import threading
from datetime import timedelta

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from apps.learning.models import UserQuestionAttempt
from apps.master_exams.models import (
    MasterExam, MasterExamQuestion, MasterExamAttempt,
)
from apps.master_exams.services import MasterExamAttemptService
from tests.factories import make_user, make_question


class ConcurrentMasterExamFinishTests(TransactionTestCase):
    """
    Two threads calling finish() on the same attempt must not
    duplicate the grading work. The select_for_update inside
    _finish_locked serializes them; the loser sees is_complete=True
    on the re-read and returns the already-finished row.

    TransactionTestCase (not TestCase) is required: the two threads
    must see each other's committed writes, which a TestCase's
    per-test transaction hides.

    SQLITE SKIP
    -----------
    SQLite uses a database-level write lock. Two concurrent write
    transactions (as here) fail with "database table is locked"
    rather than serializing on a row. `select_for_update` is a
    documented no-op on SQLite. The concurrency semantics this test
    exists to verify — row-level locking, serialized reads of the
    same attempt — do not exist on SQLite at all.

    Run this test on MariaDB / Postgres to actually exercise the
    lock:

        DJANGO_SETTINGS_MODULE=config.settings python tests/runtests.py \\
            edge_cases.test_concurrency
    """
    def setUp(self):
        super().setUp()
        if connection.vendor == 'sqlite':
            self.skipTest(
                'SQLite serializes at the database level and does not '
                'honor select_for_update; the race this test simulates '
                'cannot be reproduced. Run with MariaDB.'
            )
        self.author = make_user('author')
        self.student = make_user('student')
        self.q = make_question(
            owner=self.author, choices=['A', 'B'], correct_answer=1,
        )
        now = timezone.now()
        exam = MasterExam.objects.create(
            name='Concurrent',
            primary_attending=self.author,
            opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(hours=1),
            duration_minutes=60,
            stored_status='published',
        )
        MasterExamQuestion.objects.create(
            master_exam=exam, question=self.q, order=1,
        )
        self.exam = exam
        self.attempt = MasterExamAttemptService.start(self.student, exam)

    def tearDown(self):
        connection.close()
        super().tearDown()

    def test_two_threads_finish_do_not_double_count_srs(self):
        MasterExamAttemptService.submit_answer(self.attempt, self.q.id, 1)

        errors = []

        def _worker():
            try:
                fresh = MasterExamAttempt.objects.get(pk=self.attempt.pk)
                MasterExamAttemptService.finish(fresh, forced=False)
            except Exception as e:
                errors.append(e)
            finally:
                connection.close()

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(errors, [], f'errors: {errors}')

        final = MasterExamAttempt.objects.get(pk=self.attempt.pk)
        self.assertTrue(final.is_complete)

        # SRS row written exactly once — attempts=1, not 2.
        srs = UserQuestionAttempt.objects.get(
            user=self.student, question=self.q,
        )
        self.assertEqual(srs.attempts, 1)