# tests/api/test_master_exam_edit_api.py
"""
PUT /api/v1/exam/master/<id>/ — the update endpoint's own wrapping
logic on top of MasterExamService.update.

The service-level update tests live in
tests/master_exams/test_master_exam_service_extended.py. This file
covers the view's contribution:
  • the `expected_version` extraction from the payload
  • the field filter that drops keys the caller did not send
  • the `question_ids` rejection in the update serializer
  • the 409 translation of MODIFIED_BY_ANOTHER_USER
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from apps.master_exams.models import MasterExam, MasterExamQuestion
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


def _draft_exam(author, questions=()):
    now = timezone.now()
    exam = MasterExam.objects.create(
        name='Edit API Exam',
        primary_attending=author,
        opens_at=now + timedelta(hours=1),
        closes_at=now + timedelta(hours=2),
        duration_minutes=60,
    )
    for i, q in enumerate(questions, 1):
        MasterExamQuestion.objects.create(
            master_exam=exam, question=q, order=i,
        )
    return exam


class MasterExamUpdateAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod_user', role='moderator')
        self.q = make_question(owner=self.mod)
        self.exam = _draft_exam(self.mod, [self.q])
        self.client.force_login(self.mod)

    def test_happy_path_updates_name(self):
        resp = self.client.put(
            f'/api/v1/exam/master/{self.exam.id}/',
            {'name': 'Renamed', 'expected_version': self.exam.version},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.name, 'Renamed')

    def test_stale_version_returns_409(self):
        # Bump the version out from under the client.
        self.exam.version = 5
        self.exam.save(update_fields=['version'])
        resp = self.client.put(
            f'/api/v1/exam/master/{self.exam.id}/',
            {'name': 'Stale', 'expected_version': 1},
            format='json',
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn('إعادة التحميل', resp.json()['message'])

    def test_missing_expected_version_returns_400(self):
        resp = self.client.put(
            f'/api/v1/exam/master/{self.exam.id}/',
            {'name': 'No version'},
            format='json',
        )
        # Missing expected_version is a serializer validation error.
        self.assertEqual(resp.status_code, 400)

    def test_question_ids_in_payload_rejected(self):
        """
        The update serializer rejects `question_ids` in the body and
        tells the caller to use the dedicated add/remove/reorder
        endpoints.
        """
        resp = self.client.put(
            f'/api/v1/exam/master/{self.exam.id}/',
            {
                'question_ids': [self.q.id],
                'expected_version': self.exam.version,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('question_ids', str(resp.json()['details']))

    def test_window_started_returns_400(self):
        now = timezone.now()
        self.exam.opens_at = now - timedelta(hours=1)
        self.exam.closes_at = now + timedelta(hours=1)
        self.exam.save()
        resp = self.client.put(
            f'/api/v1/exam/master/{self.exam.id}/',
            {'name': 'Late edit', 'expected_version': self.exam.version},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_non_author_refused(self):
        other = make_user('other_mod', role='moderator')
        self.client.force_login(other)
        resp = self.client.put(
            f'/api/v1/exam/master/{self.exam.id}/',
            {'name': 'Hijack', 'expected_version': self.exam.version},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_partial_update_leaves_other_fields_alone(self):
        """
        The view filters the payload to keys the caller actually
        sent before calling the service. A PUT with only `name`
        must not clear `description`, `instructions`, or any other
        field the serializer has defaults for.
        """
        self.exam.description = 'Original description'
        self.exam.save()
        resp = self.client.put(
            f'/api/v1/exam/master/{self.exam.id}/',
            {'name': 'Renamed', 'expected_version': self.exam.version},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.description, 'Original description')