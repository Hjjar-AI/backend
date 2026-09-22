# tests/api/test_master_exam_results_api.py
import uuid
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from apps.master_exams.models import (
    MasterExam, MasterExamQuestion, MasterExamAttempt,
)
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


def _exam(author, questions):
    now = timezone.now()
    exam = MasterExam.objects.create(
        name='Results API Exam',
        primary_attending=author,
        opens_at=now - timedelta(hours=2),
        closes_at=now - timedelta(hours=1),
        duration_minutes=60,
        stored_status='published',
    )
    for i, q in enumerate(questions, 1):
        MasterExamQuestion.objects.create(
            master_exam=exam, question=q, order=i,
        )
    return exam


class ResultsViewAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.author = make_user('author', role='moderator')
        self.q = make_question(owner=self.author)
        self.exam = _exam(self.author, [self.q])

    def test_author_can_view(self):
        self.client.force_login(self.author)
        resp = self.client.get(f'/api/v1/exam/master/{self.exam.id}/results/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        for key in (
            'exam', 'summary', 'per_user', 'per_question',
            'per_category', 'per_difficulty', 'histogram', 'flags',
        ):
            self.assertIn(key, data)

    def test_anonymous_refused(self):
        resp = self.client.get(f'/api/v1/exam/master/{self.exam.id}/results/')
        self.assertIn(resp.status_code, (401, 403))

    def test_non_author_moderator_refused(self):
        other = make_user('other_mod', role='moderator')
        self.client.force_login(other)
        resp = self.client.get(f'/api/v1/exam/master/{self.exam.id}/results/')
        self.assertEqual(resp.status_code, 403)

    def test_moderator_with_view_results_any_refused_via_manage_own(self):
        """
        A moderator without `view_results_any` and not an author
        cannot see results. Moderators hold `view_results_own` by
        default, but that requires authorship.
        """
        other = make_user('other_mod', role='moderator')
        self.client.force_login(other)
        resp = self.client.get(f'/api/v1/exam/master/{self.exam.id}/results/')
        self.assertEqual(resp.status_code, 403)


class ResultsCsvViewsAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.author = make_user('author', role='moderator')
        self.q = make_question(owner=self.author)
        self.exam = _exam(self.author, [self.q])

    def test_summary_csv_returns_csv_content_type(self):
        self.client.force_login(self.author)
        resp = self.client.get(
            f'/api/v1/exam/master/{self.exam.id}/results/summary.csv/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/csv', resp['Content-Type'])
        self.assertIn('attachment', resp['Content-Disposition'])

    def test_matrix_csv_returns_csv_content_type(self):
        self.client.force_login(self.author)
        resp = self.client.get(
            f'/api/v1/exam/master/{self.exam.id}/results/matrix.csv/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/csv', resp['Content-Type'])

    def test_non_author_refused(self):
        other = make_user('outsider')
        self.client.force_login(other)
        resp = self.client.get(
            f'/api/v1/exam/master/{self.exam.id}/results/summary.csv/'
        )
        self.assertEqual(resp.status_code, 403)