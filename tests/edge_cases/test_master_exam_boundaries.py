# tests/edge_cases/test_master_exam_boundaries.py
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.utils import timezone

from apps.master_exams.models import MasterExam, MasterExamQuestion
from apps.master_exams.services import MasterExamAttemptService
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


def _exam(author, questions, opens_offset_minutes, closes_offset_minutes, **kw):
    now = timezone.now()
    defaults = {
        'name': 'Boundary Exam',
        'primary_attending': author,
        'opens_at': now + timedelta(minutes=opens_offset_minutes),
        'closes_at': now + timedelta(minutes=closes_offset_minutes),
        'duration_minutes': 60,
        'stored_status': 'published',
    }
    defaults.update(kw)
    exam = MasterExam.objects.create(**defaults)
    for i, q in enumerate(questions, 1):
        MasterExamQuestion.objects.create(
            master_exam=exam, question=q, order=i,
        )
    return exam


class MasterExamWindowBoundaries(CacheClearingTestCase):
    """
    The start path checks `now < opens_at` (block) and
    `now >= closes_at` (block unless makeup allowed).
    """
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.student = make_user('student')
        self.q = make_question(owner=self.author)

    def test_before_opens_refused(self):
        exam = _exam(self.author, [self.q], opens_offset_minutes=5, closes_offset_minutes=120)
        with self.assertRaises(ValueError) as ctx:
            MasterExamAttemptService.start(self.student, exam)
        self.assertEqual(str(ctx.exception), 'WINDOW_NOT_OPEN')

    def test_after_close_without_makeup_refused(self):
        exam = _exam(
            self.author, [self.q],
            opens_offset_minutes=-120, closes_offset_minutes=-1,
            allow_makeup=False,
        )
        with self.assertRaises(ValueError) as ctx:
            MasterExamAttemptService.start(self.student, exam)
        self.assertEqual(str(ctx.exception), 'WINDOW_CLOSED')

    def test_after_close_with_makeup_creates_makeup_attempt(self):
        exam = _exam(
            self.author, [self.q],
            opens_offset_minutes=-120, closes_offset_minutes=-1,
        )
        attempt = MasterExamAttemptService.start(self.student, exam)
        self.assertTrue(attempt.is_makeup)


class MasterExamLastAnswerBoundary(CacheClearingTestCase):
    """
    submit_answer uses `now > deadline + grace + tolerance`. At
    exactly the boundary, the answer is still accepted.
    """
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.student = make_user('student')
        self.q = make_question(owner=self.author, choices=['A', 'B'])
        self.exam = _exam(
            self.author, [self.q],
            opens_offset_minutes=-1, closes_offset_minutes=120,
        )
        self.attempt = MasterExamAttemptService.start(self.student, self.exam)

    def _boundary(self):
        return (
            settings.MASTER_EXAM_GRACE_SECONDS
            + settings.MASTER_EXAM_LAST_ANSWER_TOLERANCE_SECONDS
        )

    def test_at_boundary_accepted(self):
        # Deadline set exactly at the boundary in the past — now()
        # will be a tick past it, so use a small margin below.
        self.attempt.deadline_at = timezone.now() - timedelta(
            seconds=self._boundary() - 5,
        )
        self.attempt.save()
        MasterExamAttemptService.submit_answer(self.attempt, self.q.id, 1)
        self.attempt.refresh_from_db()
        self.assertFalse(self.attempt.is_complete)

    def test_well_past_boundary_forces_finish(self):
        self.attempt.deadline_at = timezone.now() - timedelta(
            seconds=self._boundary() + 30,
        )
        self.attempt.save()
        with self.assertRaises(ValueError) as ctx:
            MasterExamAttemptService.submit_answer(self.attempt, self.q.id, 1)
        self.assertEqual(str(ctx.exception), 'TIME_EXPIRED')
        self.attempt.refresh_from_db()
        self.assertTrue(self.attempt.is_complete)
        self.assertTrue(self.attempt.forced_finish)


class CurrentQuestionReadTests(CacheClearingTestCase):
    def test_valid_current_question_is_not_resaved(self):
        author = make_user('author')
        student = make_user('student')
        question = make_question(owner=author)
        exam = _exam(
            author, [question],
            opens_offset_minutes=-1, closes_offset_minutes=120,
        )
        attempt = MasterExamAttemptService.start(student, exam)

        with patch.object(attempt, 'save') as save:
            payload = MasterExamAttemptService.current_question(attempt)

        save.assert_not_called()
        self.assertEqual(payload['question']['id'], question.id)
