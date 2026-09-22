# tests/master_exams/test_results_service.py
"""
MasterExamResultsService — the dashboard a moderator opens after
running a master exam. Highest blast radius of the uncovered
services: real aggregation, weighted score math, CSV emission with
formula-injection sanitization.
"""
import csv
import io
import uuid
from datetime import timedelta

from django.utils import timezone

from apps.feedback.models import QuestionFlag
from apps.master_exams.models import (
    MasterExam, MasterExamQuestion, MasterExamAttempt,
)
from apps.master_exams.services import MasterExamResultsService
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


def _exam(author, questions, **kw):
    now = timezone.now()
    defaults = {
        'name': 'Results Exam',
        'primary_attending': author,
        'opens_at': now - timedelta(hours=2),
        'closes_at': now - timedelta(hours=1),
        'duration_minutes': 60,
        'stored_status': 'published',
        'audience_all_doctors': True,
    }
    defaults.update(kw)
    exam = MasterExam.objects.create(**defaults)
    for i, q in enumerate(questions, 1):
        MasterExamQuestion.objects.create(
            master_exam=exam, question=q, order=i,
        )
    return exam


def _attempt(exam, user, answers=None, **kw):
    """
    Create a finished attempt directly. `answers` is the master-exam
    shape: { str(question_id): {'answer': N, 'confidence': bool} }.
    """
    now = timezone.now()
    qids = list(
        exam.exam_questions
        .order_by('order')
        .values_list('question_id', flat=True)
    )
    defaults = {
        'master_exam': exam,
        'user': user,
        'session_id': str(uuid.uuid4()),
        'question_ids': qids,
        'answers': answers or {},
        'current_question_id': qids[0] if qids else None,
        'started_at': now - timedelta(minutes=30),
        'deadline_at': now + timedelta(minutes=30),
        'finished_at': now,
        'results': {'questions': []},
        'correct_count': 0,
        'total_questions': len(qids),
        'accuracy': 0.0,
        'weighted_score': 0.0,
        'is_complete': True,
        'exam_name_snapshot': exam.name,
    }
    defaults.update(kw)
    return MasterExamAttempt.objects.create(**defaults)


class LiveSummaryTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.q1 = make_question(owner=self.author, choices=['A', 'B'])
        self.exam = _exam(self.author, [self.q1])

    def test_no_audience_no_attempts(self):
        self.exam.audience_all_doctors = False
        self.exam.save()
        summary = MasterExamResultsService.live_summary(self.exam)
        self.assertEqual(summary['total_assigned'], 0)
        self.assertEqual(summary['started'], 0)
        self.assertEqual(summary['finished'], 0)
        self.assertEqual(summary['not_started'], 0)
        self.assertEqual(summary['avg_accuracy'], 0.0)

    def test_audience_and_attempt_counts(self):
        alice = make_user('alice')
        bob = make_user('bob')
        carol = make_user('carol')

        _attempt(self.exam, alice, accuracy=80.0, correct_count=1)
        # In-progress: started but not complete.
        MasterExamAttempt.objects.create(
            master_exam=self.exam,
            user=bob,
            session_id=str(uuid.uuid4()),
            question_ids=[self.q1.id],
            answers={},
            started_at=timezone.now(),
            deadline_at=timezone.now() + timedelta(minutes=30),
            is_complete=False,
            exam_name_snapshot=self.exam.name,
        )
        # carol never started.

        summary = MasterExamResultsService.live_summary(self.exam)
        self.assertEqual(summary['total_assigned'], 3)
        self.assertEqual(summary['started'], 2)
        self.assertEqual(summary['finished'], 1)
        self.assertEqual(summary['in_progress'], 1)
        self.assertEqual(summary['not_started'], 1)

    def test_avg_accuracy_only_over_finished(self):
        alice = make_user('alice')
        bob = make_user('bob')
        _attempt(self.exam, alice, accuracy=80.0)
        _attempt(self.exam, bob, accuracy=60.0)
        summary = MasterExamResultsService.live_summary(self.exam)
        self.assertEqual(summary['avg_accuracy'], 70.0)


class PerUserRowsTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.q1 = make_question(owner=self.author, choices=['A', 'B'])
        self.exam = _exam(self.author, [self.q1])

    def test_empty_when_no_attempts(self):
        self.assertEqual(
            MasterExamResultsService.per_user_rows(self.exam), [],
        )

    def test_sorted_by_accuracy_descending(self):
        alice = make_user('alice')
        bob = make_user('bob')
        _attempt(self.exam, alice, accuracy=50.0, correct_count=1)
        _attempt(self.exam, bob, accuracy=90.0, correct_count=1)
        rows = MasterExamResultsService.per_user_rows(self.exam)
        self.assertEqual([r['username'] for r in rows], ['bob', 'alice'])

    def test_fields_present(self):
        alice = make_user('alice')
        _attempt(
            self.exam, alice,
            accuracy=75.5, correct_count=1, weighted_score=80.0,
            is_makeup=True, forced_finish=True,
        )
        row = MasterExamResultsService.per_user_rows(self.exam)[0]
        for key in (
            'user_id', 'username', 'full_name', 'is_complete',
            'is_makeup', 'forced_finish', 'correct_count',
            'total_questions', 'accuracy', 'weighted_score',
            'started_at', 'finished_at',
        ):
            self.assertIn(key, row)
        self.assertEqual(row['accuracy'], 75.5)
        self.assertTrue(row['is_makeup'])
        self.assertTrue(row['forced_finish'])


class PerQuestionStatsTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.q1 = make_question(
            owner=self.author, choices=['A', 'B', 'C'], correct_answer=1,
        )
        self.q2 = make_question(
            owner=self.author, choices=['A', 'B', 'C'], correct_answer=2,
        )
        self.exam = _exam(self.author, [self.q1, self.q2])

    def test_no_completed_attempts_yields_zeroes(self):
        rows = MasterExamResultsService.per_question_stats(self.exam)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['answered_count'], 0)
        self.assertEqual(rows[0]['correct_rate'], 0.0)

    def test_distribution_and_correct_rate(self):
        alice = make_user('alice')
        bob = make_user('bob')
        _attempt(
            self.exam, alice,
            answers={
                str(self.q1.id): {'answer': 1, 'confidence': True},  # correct
                str(self.q2.id): {'answer': 1, 'confidence': True},  # wrong
            },
        )
        _attempt(
            self.exam, bob,
            answers={
                str(self.q1.id): {'answer': 2, 'confidence': True},  # wrong
                str(self.q2.id): {'answer': 2, 'confidence': True},  # correct
            },
        )

        rows = MasterExamResultsService.per_question_stats(self.exam)
        q1_row = next(r for r in rows if r['question_id'] == self.q1.id)
        self.assertEqual(q1_row['answered_count'], 2)
        self.assertEqual(q1_row['correct_count'], 1)
        self.assertEqual(q1_row['correct_rate'], 50.0)
        self.assertEqual(q1_row['distribution'], {'1': 1, '2': 1})

    def test_deleted_question_marked(self):
        # Delete q2 while it is still referenced by the through-row.
        # MasterExamQuestion uses PROTECT, so delete the through-row first.
        MasterExamQuestion.objects.filter(question=self.q2).delete()
        self.q2.delete()

        rows = MasterExamResultsService.per_question_stats(self.exam)
        # Only q1 remains in the ordered id list.
        self.assertEqual(len(rows), 1)


class PerCategoryStatsTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        from tests.factories import make_category
        self.author = make_user('author')
        self.cat = make_category('cat-a')
        self.q = make_question(
            owner=self.author, category=self.cat,
            choices=['A', 'B'], correct_answer=1,
        )
        self.exam = _exam(self.author, [self.q])

    def test_correct_rate_per_category(self):
        alice = make_user('alice')
        bob = make_user('bob')
        _attempt(
            self.exam, alice,
            answers={str(self.q.id): {'answer': 1}},
        )
        _attempt(
            self.exam, bob,
            answers={str(self.q.id): {'answer': 2}},
        )
        rows = MasterExamResultsService.per_category_stats(self.exam)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['category_name'], 'cat-a')
        self.assertEqual(row['answered'], 2)
        self.assertEqual(row['correct'], 1)
        self.assertEqual(row['correct_rate'], 50.0)


class PerDifficultyStatsTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.q_easy = make_question(
            owner=self.author, difficulty='easy', choices=['A', 'B'],
        )
        self.q_hard = make_question(
            owner=self.author, difficulty='hard', choices=['A', 'B'],
        )
        self.exam = _exam(self.author, [self.q_easy, self.q_hard])

    def test_returns_all_three_tiers(self):
        rows = MasterExamResultsService.per_difficulty_stats(self.exam)
        tiers = [r['difficulty'] for r in rows]
        self.assertEqual(tiers, ['easy', 'medium', 'hard'])

    def test_counts_per_tier(self):
        alice = make_user('alice')
        _attempt(
            self.exam, alice,
            answers={
                str(self.q_easy.id): {'answer': 1},
                str(self.q_hard.id): {'answer': 2},
            },
        )
        rows = MasterExamResultsService.per_difficulty_stats(self.exam)
        easy = next(r for r in rows if r['difficulty'] == 'easy')
        hard = next(r for r in rows if r['difficulty'] == 'hard')
        medium = next(r for r in rows if r['difficulty'] == 'medium')
        self.assertEqual(easy['answered'], 1)
        self.assertEqual(easy['correct'], 1)
        self.assertEqual(hard['answered'], 1)
        self.assertEqual(hard['correct'], 0)
        self.assertEqual(medium['answered'], 0)


class ScoreHistogramTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.q = make_question(owner=self.author)
        self.exam = _exam(self.author, [self.q])

    def test_empty_histogram(self):
        rows = MasterExamResultsService.score_histogram(self.exam)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r['count'] == 0 for r in rows))

    def test_buckets_assigned_correctly(self):
        for username, acc in (
            ('user_10', 10.0),
            ('user_30', 30.0),
            ('user_50', 50.0),
            ('user_70', 70.0),
            ('user_90', 90.0),
        ):
            u = make_user(username)
            _attempt(self.exam, u, accuracy=acc)

        rows = MasterExamResultsService.score_histogram(self.exam)
        buckets = {r['range']: r['count'] for r in rows}
        self.assertEqual(buckets['0-20'], 1)
        self.assertEqual(buckets['20-40'], 1)
        self.assertEqual(buckets['40-60'], 1)
        self.assertEqual(buckets['60-80'], 1)
        self.assertEqual(buckets['80-100'], 1)

    def test_boundary_100_goes_to_top_bucket(self):
        alice = make_user('alice')
        _attempt(self.exam, alice, accuracy=100.0)
        rows = MasterExamResultsService.score_histogram(self.exam)
        top = next(r for r in rows if r['range'] == '80-100')
        self.assertEqual(top['count'], 1)


class FlagsRaisedTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.q = make_question(owner=self.author)
        self.exam = _exam(self.author, [self.q])
        self.student = make_user('student')
        self.attempt = _attempt(self.exam, self.student)

    def test_empty_when_no_flags(self):
        self.assertEqual(
            MasterExamResultsService.flags_raised_during_exam(self.exam), [],
        )

    def test_open_flag_listed(self):
        QuestionFlag.objects.create(
            question=self.q, user=self.student,
            reason='unclear wording',
            master_exam_attempt=self.attempt,
        )
        rows = MasterExamResultsService.flags_raised_during_exam(self.exam)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['flagger_username'], 'student')
        self.assertEqual(rows[0]['reason'], 'unclear wording')

    def test_resolved_flag_excluded(self):
        QuestionFlag.objects.create(
            question=self.q, user=self.student,
            reason='old', resolved=True,
            master_exam_attempt=self.attempt,
        )
        self.assertEqual(
            MasterExamResultsService.flags_raised_during_exam(self.exam), [],
        )


class CsvSummaryTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.q = make_question(owner=self.author)
        self.exam = _exam(self.author, [self.q])

    def test_header_row_present(self):
        body = MasterExamResultsService.csv_summary(self.exam)
        reader = csv.reader(io.StringIO(body))
        header = next(reader)
        self.assertEqual(header, [
            'full_name', 'username', 'score', 'correct_count', 'total',
            'accuracy', 'weighted_score', 'is_makeup', 'forced_finish',
            'started_at', 'finished_at',
        ])

    def test_data_row_for_finished_attempt(self):
        alice = make_user('alice')
        _attempt(self.exam, alice, accuracy=80.0, correct_count=1)
        body = MasterExamResultsService.csv_summary(self.exam)
        reader = csv.reader(io.StringIO(body))
        next(reader)
        row = next(reader)
        # score column is "correct/total" for finished attempts.
        self.assertEqual(row[2], '1/1')
        self.assertIn('80.0', row[5])

    def test_incomplete_attempt_marked(self):
        alice = make_user('alice')
        MasterExamAttempt.objects.create(
            master_exam=self.exam, user=alice,
            session_id=str(uuid.uuid4()),
            question_ids=[self.q.id], answers={},
            started_at=timezone.now(),
            deadline_at=timezone.now() + timedelta(minutes=30),
            is_complete=False,
            exam_name_snapshot=self.exam.name,
        )
        body = MasterExamResultsService.csv_summary(self.exam)
        self.assertIn('(لم يكمل)', body)

    def test_formula_injection_sanitized(self):
        # A user's full_name beginning with '=' must be escaped.
        alice = make_user('alice', full_name='=HYPERLINK("evil")')
        _attempt(self.exam, alice)
        body = MasterExamResultsService.csv_summary(self.exam)
        self.assertIn("'=HYPERLINK", body)


class CsvMatrixTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.q1 = make_question(
            owner=self.author, choices=['A', 'B'], correct_answer=1,
        )
        self.q2 = make_question(
            owner=self.author, choices=['A', 'B'], correct_answer=2,
        )
        self.exam = _exam(self.author, [self.q1, self.q2])

    def test_matrix_has_question_columns(self):
        alice = make_user('alice')
        _attempt(
            self.exam, alice,
            answers={
                str(self.q1.id): {'answer': 1},  # correct
                str(self.q2.id): {'answer': 1},  # wrong
            },
        )
        body = MasterExamResultsService.csv_matrix(self.exam)
        reader = csv.reader(io.StringIO(body))
        header = next(reader)
        self.assertEqual(header, ['full_name', 'username', 'Q1', 'Q2'])
        row = next(reader)
        self.assertEqual(row[0], 'alice')
        self.assertEqual(row[2], '✓')
        self.assertEqual(row[3], '✗')

    def test_missing_answer_renders_dash(self):
        alice = make_user('alice')
        _attempt(
            self.exam, alice,
            answers={str(self.q1.id): {'answer': 1}},
        )
        body = MasterExamResultsService.csv_matrix(self.exam)
        reader = csv.reader(io.StringIO(body))
        next(reader)
        row = next(reader)
        self.assertEqual(row[2], '✓')
        self.assertEqual(row[3], '—')