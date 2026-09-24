# tests/learning/test_srs_service.py
from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.learning.models import UserQuestionAttempt
from apps.learning.srs_service import (
    SRSService, _apply_review, _next_interval, EASE_FLOOR,
)
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


class NextIntervalTests(CacheClearingTestCase):
    def test_first_repetition_is_one_day(self):
        self.assertEqual(_next_interval(1, 0, 2.5), 1)

    def test_second_repetition_is_six_days(self):
        self.assertEqual(_next_interval(2, 1, 2.5), 6)

    def test_third_repetition_multiplies_by_ease(self):
        self.assertEqual(_next_interval(3, 6, 2.5), 15)
        self.assertEqual(_next_interval(3, 6, 3.0), 18)

    def test_interval_never_below_one(self):
        self.assertGreaterEqual(_next_interval(3, 0, 1.3), 1)


class ApplyReviewTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.q = make_question(owner=self.user)

    def _fresh(self):
        return UserQuestionAttempt(user=self.user, question=self.q)

    def test_correct_confident_advances_ladder(self):
        a = self._fresh()
        _apply_review(a, is_correct=True, is_confident=True)
        self.assertEqual(a.attempts, 1)
        self.assertEqual(a.repetitions, 1)
        self.assertEqual(a.interval_days, 1)
        self.assertTrue(a.ever_correct)

    def test_numeric_confidence_score_is_preserved(self):
        a = self._fresh()
        _apply_review(a, is_correct=True, confidence_score=2)
        self.assertEqual(a.last_confidence_score, 2)
        self.assertFalse(a.last_confidence)

    def test_guessing_score_is_preserved(self):
        a = self._fresh()
        _apply_review(a, is_correct=False, confidence_score=1)
        self.assertEqual(a.last_confidence_score, 1)
        self.assertFalse(a.last_confidence)

    def test_wrong_unknown_resets_to_one_day(self):
        a = self._fresh()
        a.repetitions = 5
        a.interval_days = 30
        _apply_review(a, is_correct=False, is_confident=False,
                      error_reason='unknown')
        self.assertEqual(a.repetitions, 0)
        self.assertEqual(a.interval_days, 1)
        self.assertEqual(a.wrong_count, 1)

    def test_wrong_misread_keeps_repetitions(self):
        """
        Knowledge is present; the reading habit is not. Repetitions
        do not reset; a short re-show is scheduled.
        """
        a = self._fresh()
        a.repetitions = 3
        _apply_review(a, is_correct=False, is_confident=False,
                      error_reason='misread')
        self.assertEqual(a.repetitions, 3)
        self.assertEqual(a.interval_days, 2)

    def test_wrong_confused_steps_back_one(self):
        a = self._fresh()
        a.repetitions = 3
        _apply_review(a, is_correct=False, is_confident=False,
                      error_reason='confused')
        self.assertEqual(a.repetitions, 2)
        self.assertEqual(a.interval_days, 3)

    def test_wrong_guessed_resets(self):
        a = self._fresh()
        a.repetitions = 4
        _apply_review(a, is_correct=False, is_confident=False,
                      error_reason='guessed')
        self.assertEqual(a.repetitions, 0)
        self.assertEqual(a.interval_days, 1)

    def test_ease_factor_has_a_floor(self):
        a = self._fresh()
        a.ease_factor = EASE_FLOOR
        for _ in range(10):
            _apply_review(a, is_correct=False, is_confident=False)
        self.assertGreaterEqual(a.ease_factor, EASE_FLOOR)

    def test_correct_clears_error_reason(self):
        a = self._fresh()
        _apply_review(a, is_correct=False, is_confident=False,
                      error_reason='misread')
        self.assertEqual(a.last_error_reason, 'misread')
        _apply_review(a, is_correct=True, is_confident=True)
        self.assertIsNone(a.last_error_reason)


class RecordAttemptsBulkTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.q1 = make_question(owner=self.user)
        self.q2 = make_question(owner=self.user)

    def _results(self):
        return [
            {
                'question_id': self.q1.id,
                'user_answer': 1,
                'is_correct': True,
                'confidence': True,
                'error_reason': None,
            },
            {
                'question_id': self.q2.id,
                'user_answer': 2,
                'is_correct': False,
                'confidence': False,
                'error_reason': 'misread',
            },
        ]

    def test_creates_rows_for_new_attempts(self):
        count = SRSService.record_attempts_bulk(self.user, self._results())
        self.assertEqual(count, 2)
        self.assertEqual(UserQuestionAttempt.objects.count(), 2)

    def test_unanswered_rows_are_skipped(self):
        results = [{
            'question_id': self.q1.id,
            'user_answer': None,
            'is_correct': False,
            'confidence': True,
            'error_reason': None,
        }]
        count = SRSService.record_attempts_bulk(self.user, results)
        self.assertEqual(count, 0)

    def test_second_call_updates_existing(self):
        SRSService.record_attempts_bulk(self.user, self._results())
        SRSService.record_attempts_bulk(self.user, self._results())
        a = UserQuestionAttempt.objects.get(user=self.user, question=self.q1)
        self.assertEqual(a.attempts, 2)

    def test_race_falls_back_to_update_or_create(self):
        """
        Simulate the race the savepoint handler exists for: the row
        is inserted by a concurrent worker between our read and our
        bulk_create. The IntegrityError must be caught, the outer
        transaction must survive, and the fallback must run.
        """
        # Pre-create the row so the bulk_create inside the service
        # will hit the unique constraint.
        UserQuestionAttempt.objects.create(
            user=self.user, question=self.q1,
        )

        real_bulk_create = UserQuestionAttempt.objects.bulk_create
        call_count = {'n': 0}

        def _side_effect(objs, **kw):
            call_count['n'] += 1
            if call_count['n'] == 1:
                raise IntegrityError('simulated race')
            return real_bulk_create(objs, **kw)

        with patch.object(
            UserQuestionAttempt.objects, 'bulk_create',
            side_effect=_side_effect,
        ):
            with transaction.atomic():
                SRSService.record_attempts_bulk(
                    self.user, self._results(),
                )

        a = UserQuestionAttempt.objects.get(user=self.user, question=self.q1)
        self.assertEqual(a.attempts, 1)  # the service's own review


class DueAndQueryTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.q1 = make_question(owner=self.user)
        self.q2 = make_question(owner=self.user)

    def test_due_includes_past_and_null_next_due(self):
        now = timezone.now()
        UserQuestionAttempt.objects.create(
            user=self.user, question=self.q1,
            next_due=now - timedelta(days=1),
        )
        UserQuestionAttempt.objects.create(
            user=self.user, question=self.q2,
            next_due=None,
        )
        due = set(SRSService.due_question_ids(self.user))
        self.assertEqual(due, {self.q1.id, self.q2.id})

    def test_due_excludes_future(self):
        UserQuestionAttempt.objects.create(
            user=self.user, question=self.q1,
            next_due=timezone.now() + timedelta(days=7),
        )
        self.assertEqual(SRSService.due_question_ids(self.user), [])

    def test_wrong_question_ids_ordered_by_wrong_count(self):
        UserQuestionAttempt.objects.create(
            user=self.user, question=self.q1,
            ever_correct=False, wrong_count=1,
        )
        UserQuestionAttempt.objects.create(
            user=self.user, question=self.q2,
            ever_correct=False, wrong_count=5,
        )
        wrong = SRSService.wrong_question_ids(self.user)
        self.assertEqual(wrong[0], self.q2.id)

    def test_fragile_question_ids_are_correct_but_not_confident(self):
        UserQuestionAttempt.objects.create(
            user=self.user, question=self.q1,
            last_correct=True, last_confidence=False,
        )
        UserQuestionAttempt.objects.create(
            user=self.user, question=self.q2,
            last_correct=True, last_confidence=True,
        )
        self.assertEqual(
            SRSService.fragile_question_ids(self.user), [self.q1.id],
        )

    def test_attempt_summary_shape(self):
        UserQuestionAttempt.objects.create(
            user=self.user, question=self.q1,
            ever_correct=True, last_correct=True, last_confidence=False,
        )
        summary = SRSService.attempt_summary(self.user)
        self.assertEqual(summary['total_seen'], 1)
        self.assertEqual(summary['ever_correct'], 1)
        self.assertEqual(summary['fragile_correct'], 1)
        self.assertEqual(summary['wrong_open'], 0)
