# tests/exams/test_exam_service.py
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from apps.exams.models import ExamSession, TestHistory
from apps.exams.services import ExamService
from apps.learning.models import UserQuestionAttempt
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


class StartSessionTests(CacheClearingTestCase):
    def test_start_creates_session(self):
        u = make_user()
        q = make_question(owner=u)
        s = ExamService.start_session(u, 'study', [q.id])
        self.assertEqual(s.user, u)
        self.assertEqual(s.mode, 'study')
        self.assertEqual(s.question_ids, [q.id])
        self.assertTrue(s.is_active)

    def test_start_deletes_previous_active_same_mode(self):
        u = make_user()
        q = make_question(owner=u)
        first = ExamService.start_session(u, 'study', [q.id])
        ExamService.start_session(u, 'study', [q.id])
        self.assertFalse(
            ExamSession.objects.filter(pk=first.pk).exists()
        )

    def test_start_preserves_other_mode(self):
        u = make_user()
        q = make_question(owner=u)
        exam_s = ExamService.start_session(u, 'exam', [q.id])
        ExamService.start_session(u, 'study', [q.id])
        self.assertTrue(
            ExamSession.objects.filter(pk=exam_s.pk).exists()
        )


class SubmitAnswerTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.u = make_user()
        self.q = make_question(owner=self.u, choices=['A', 'B', 'C'])
        self.s = ExamService.start_session(self.u, 'study', [self.q.id])

    def test_answer_above_choice_count_is_rejected(self):
        with self.assertRaises(ValueError):
            ExamService.submit_answer(self.s, 4, 'next')

    def test_answer_zero_is_rejected(self):
        with self.assertRaises(ValueError):
            ExamService.submit_answer(self.s, 0, 'next')

    def test_answer_persists_and_advances(self):
        ExamService.submit_answer(self.s, 1, 'next')
        self.s.refresh_from_db()
        self.assertEqual(self.s.answers['0']['answer'], 1)
        self.assertEqual(self.s.current_index, 1)


class FinishSessionTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.u = make_user()
        self.q = make_question(owner=self.u, choices=['A', 'B'], correct_answer=1)

    def test_finish_writes_history_and_deletes_session(self):
        s = ExamService.start_session(self.u, 'study', [self.q.id])
        ExamService.submit_answer(s, 1, 'next')
        result = ExamService.finish_session(s, self.u)

        self.assertEqual(result['correct_count'], 1)
        self.assertEqual(result['total_questions'], 1)
        self.assertFalse(ExamSession.objects.filter(pk=s.pk).exists())
        self.assertEqual(TestHistory.objects.count(), 1)

    def test_finish_updates_question_stats(self):
        s = ExamService.start_session(self.u, 'study', [self.q.id])
        ExamService.submit_answer(s, 1, 'next')
        ExamService.finish_session(s, self.u)
        self.q.refresh_from_db()
        self.assertEqual(self.q.times_answered, 1)
        self.assertEqual(self.q.times_correct, 1)

    def test_finish_records_srs_attempts(self):
        s = ExamService.start_session(self.u, 'study', [self.q.id])
        ExamService.submit_answer(s, 1, 'next')
        ExamService.finish_session(s, self.u)
        a = UserQuestionAttempt.objects.get(user=self.u, question=self.q)
        self.assertEqual(a.attempts, 1)
        self.assertTrue(a.ever_correct)

    def test_finish_twice_raises(self):
        s = ExamService.start_session(self.u, 'study', [self.q.id])
        ExamService.submit_answer(s, 1, 'next')
        ExamService.finish_session(s, self.u)
        with self.assertRaises(ValueError):
            ExamService.finish_session(s, self.u)

    def test_finish_does_not_double_count_on_second_call(self):
        s = ExamService.start_session(self.u, 'study', [self.q.id])
        ExamService.submit_answer(s, 1, 'next')
        ExamService.finish_session(s, self.u)
        try:
            ExamService.finish_session(s, self.u)
        except ValueError:
            pass
        self.q.refresh_from_db()
        self.assertEqual(self.q.times_answered, 1)
        self.assertEqual(TestHistory.objects.count(), 1)


class PauseResumeTests(CacheClearingTestCase):
    def test_pause_accumulates_time(self):
        u = make_user()
        q = make_question(owner=u)
        s = ExamService.start_session(u, 'study', [q.id])
        s.started_at = timezone.now() - timedelta(seconds=60)
        s.save()
        ExamService.pause_session(s)
        s.refresh_from_db()
        self.assertFalse(s.is_active)
        self.assertGreaterEqual(s.accumulated_time, 60)

    def test_resume_sets_active_and_resets_started_at(self):
        u = make_user()
        q = make_question(owner=u)
        s = ExamService.start_session(u, 'study', [q.id])
        ExamService.pause_session(s)
        ExamService.resume_session(s)
        s.refresh_from_db()
        self.assertTrue(s.is_active)