# tests/api/test_master_exam_api.py
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from apps.master_exams.models import (
    MasterExam, MasterExamQuestion, MasterExamAttempt,
)
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


def _make_exam(author, questions, **kw):
    now = timezone.now()
    defaults = {
        'name': 'Test Exam',
        'primary_attending': author,
        'opens_at': now - timedelta(hours=1),
        'closes_at': now + timedelta(hours=1),
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


class MasterExamHTTPLifecycleTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.author = make_user('author')
        self.student = make_user('student')
        self.q1 = make_question(owner=self.author, choices=['A', 'B'])
        self.exam = _make_exam(
            self.author, [self.q1], audience_all_doctors=True,
        )

    def test_anonymous_cannot_list(self):
        resp = self.client.get('/api/v1/exam/master/')
        self.assertIn(resp.status_code, (401, 403))

    def test_full_attempt_via_http(self):
        self.client.force_login(self.student)

        resp = self.client.get('/api/v1/exam/master/')
        self.assertEqual(resp.status_code, 200)

        resp = self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/start/',
            {'preview': False},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        attempt_id = resp.json()['data']['id']

        resp = self.client.get(
            f'/api/v1/exam/master/{self.exam.id}/attempt/question/'
        )
        self.assertEqual(resp.status_code, 200)

        resp = self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/attempt/answer/',
            {'question_id': self.q1.id, 'answer': 1, 'confidence': True},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)

        resp = self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/attempt/finish/',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['correct_count'], 1)

        attempt = MasterExamAttempt.objects.get(id=attempt_id)
        self.assertTrue(attempt.is_complete)

    def test_foreign_question_answer_returns_400_with_code(self):
        self.client.force_login(self.student)
        self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/start/',
            {'preview': False},
            format='json',
        )
        other_q = make_question(owner=self.author)
        resp = self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/attempt/answer/',
            {'question_id': other_q.id, 'answer': 1},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json()['details']['code'], 'QUESTION_NOT_IN_ATTEMPT',
        )


class MasterExamAuthoringAPITests(CacheClearingTestCase):
    """
    Authoring endpoints are gated by `master_exams.create` /
    `manage_own`, which the `moderator` role holds and the `member`
    role does not. The authoring user in this class is therefore a
    moderator.
    """
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.author = make_user('author', role='moderator')
        self.client.force_login(self.author)
        self.q1 = make_question(owner=self.author)
        self.q2 = make_question(owner=self.author)

    def _create_payload(self, **kw):
        now = timezone.now()
        base = {
            'name': 'New Exam',
            'opens_at': (now + timedelta(hours=1)).isoformat(),
            'closes_at': (now + timedelta(hours=2)).isoformat(),
            'duration_minutes': 60,
            'question_ids': [self.q1.id],
        }
        base.update(kw)
        return base

    def test_create_starts_as_draft(self):
        resp = self.client.post(
            '/api/v1/exam/master/', self._create_payload(), format='json',
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        exam_id = resp.json()['data']['id']
        exam = MasterExam.objects.get(id=exam_id)
        self.assertEqual(exam.stored_status, 'draft')

    def test_publish_transitions_status(self):
        resp = self.client.post(
            '/api/v1/exam/master/', self._create_payload(), format='json',
        )
        exam_id = resp.json()['data']['id']
        resp = self.client.post(f'/api/v1/exam/master/{exam_id}/publish/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            MasterExam.objects.get(id=exam_id).stored_status, 'published',
        )

    def test_closes_before_opens_rejected(self):
        now = timezone.now()
        payload = self._create_payload(
            opens_at=(now + timedelta(hours=2)).isoformat(),
            closes_at=(now + timedelta(hours=1)).isoformat(),
        )
        resp = self.client.post(
            '/api/v1/exam/master/', payload, format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_duration_longer_than_window_rejected(self):
        now = timezone.now()
        payload = self._create_payload(
            opens_at=now.isoformat(),
            closes_at=(now + timedelta(minutes=30)).isoformat(),
            duration_minutes=90,
        )
        resp = self.client.post(
            '/api/v1/exam/master/', payload, format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_add_question_to_draft(self):
        resp = self.client.post(
            '/api/v1/exam/master/', self._create_payload(), format='json',
        )
        exam_id = resp.json()['data']['id']
        resp = self.client.post(
            f'/api/v1/exam/master/{exam_id}/questions/add/',
            {'question_ids': [self.q2.id]},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            MasterExam.objects.get(id=exam_id).exam_questions.count(), 2,
        )

    def test_non_author_cannot_edit(self):
        resp = self.client.post(
            '/api/v1/exam/master/', self._create_payload(), format='json',
        )
        exam_id = resp.json()['data']['id']
        # A moderator who is not an author of this exam and does not
        # hold 'manage_any' is refused. (member would also be refused,
        # but a member's 403 would not distinguish authorship from
        # capability.)
        other = make_user('other_mod', role='moderator')
        self.client.force_login(other)
        resp = self.client.put(
            f'/api/v1/exam/master/{exam_id}/',
            {'name': 'hijacked', 'expected_version': 1},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_member_cannot_create_exam(self):
        """
        A member does not hold 'master_exams.create'. This is the
        capability that was silently missing from my first draft of
        this file.
        """
        member = make_user('member', role='member')
        self.client.force_login(member)
        resp = self.client.post(
            '/api/v1/exam/master/', self._create_payload(), format='json',
        )
        self.assertEqual(resp.status_code, 403)