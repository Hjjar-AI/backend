# tests/api/test_learning_api.py
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from apps.learning.models import UserQuestionAttempt
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


class WrongAnswersAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_empty_when_no_wrong_answers(self):
        resp = self.client.get('/api/v1/questions/mistakes/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['items'], [])

    def test_lists_wrong_open_questions(self):
        q1 = make_question(owner=self.u)
        q2 = make_question(owner=self.u)
        UserQuestionAttempt.objects.create(
            user=self.u, question=q1, ever_correct=False,
        )
        UserQuestionAttempt.objects.create(
            user=self.u, question=q2, ever_correct=True,
        )
        resp = self.client.get('/api/v1/questions/mistakes/')
        ids = {item['id'] for item in resp.json()['data']['items']}
        self.assertEqual(ids, {q1.id})


class FragileAnswersAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_lists_correct_but_not_confident(self):
        q1 = make_question(owner=self.u)
        q2 = make_question(owner=self.u)
        UserQuestionAttempt.objects.create(
            user=self.u, question=q1,
            last_correct=True, last_confidence=False,
        )
        UserQuestionAttempt.objects.create(
            user=self.u, question=q2,
            last_correct=True, last_confidence=True,
        )
        resp = self.client.get('/api/v1/questions/fragile/')
        ids = {item['id'] for item in resp.json()['data']['items']}
        self.assertEqual(ids, {q1.id})


class AttemptSummaryAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_empty_summary(self):
        resp = self.client.get('/api/v1/questions/attempt-summary/')
        data = resp.json()['data']
        self.assertEqual(data['total_seen'], 0)
        self.assertEqual(data['ever_correct'], 0)
        self.assertEqual(data['due_now'], 0)

    def test_summary_counts(self):
        """
        `due_now` counts every row whose next_due is in the past OR
        NULL. The default `next_due` for a freshly-created attempt is
        NULL, which counts as due. Rows in this test therefore set
        an explicit future `next_due` unless they are meant to be due.
        """
        q1 = make_question(owner=self.u)
        q2 = make_question(owner=self.u)
        q3 = make_question(owner=self.u)
        future = timezone.now() + timedelta(days=7)
        past = timezone.now() - timedelta(days=1)

        UserQuestionAttempt.objects.create(
            user=self.u, question=q1,
            ever_correct=True, last_correct=True, last_confidence=False,
            next_due=future,
        )
        UserQuestionAttempt.objects.create(
            user=self.u, question=q2,
            ever_correct=False,
            next_due=future,
        )
        UserQuestionAttempt.objects.create(
            user=self.u, question=q3,
            ever_correct=True, last_correct=True, last_confidence=True,
            next_due=past,
        )
        resp = self.client.get('/api/v1/questions/attempt-summary/')
        data = resp.json()['data']
        self.assertEqual(data['total_seen'], 3)
        self.assertEqual(data['ever_correct'], 2)
        self.assertEqual(data['wrong_open'], 1)
        self.assertEqual(data['fragile_correct'], 1)
        self.assertEqual(data['due_now'], 1)


class SRSDueCountAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_zero_when_no_due(self):
        resp = self.client.get('/api/v1/questions/srs-due-count/')
        self.assertEqual(resp.json()['data']['count'], 0)

    def test_counts_due_and_null_next_due(self):
        q1 = make_question(owner=self.u)
        q2 = make_question(owner=self.u)
        UserQuestionAttempt.objects.create(
            user=self.u, question=q1,
            next_due=timezone.now() - timedelta(hours=1),
        )
        UserQuestionAttempt.objects.create(
            user=self.u, question=q2,
            next_due=None,
        )
        resp = self.client.get('/api/v1/questions/srs-due-count/')
        self.assertEqual(resp.json()['data']['count'], 2)


class StudyNowAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_empty_payload_on_empty_bank(self):
        resp = self.client.get('/api/v1/questions/study-now/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(data['question_ids'], [])
        self.assertEqual(data['total'], 0)
        self.assertIn('breakdown', data)

    def test_returns_fresh_questions_for_new_user(self):
        for _ in range(30):
            make_question(owner=self.u)
        resp = self.client.get('/api/v1/questions/study-now/?limit=10')
        data = resp.json()['data']
        self.assertEqual(data['total'], 10)
        self.assertEqual(data['breakdown']['fresh'], 10)

    def test_limit_clamped_to_200(self):
        for _ in range(300):
            make_question(owner=self.u)
        resp = self.client.get('/api/v1/questions/study-now/?limit=9999')
        self.assertLessEqual(resp.json()['data']['total'], 200)