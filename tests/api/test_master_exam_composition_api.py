# tests/api/test_master_exam_composition_api.py
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from apps.master_exams.models import (
    MasterExam, MasterExamQuestion, MasterExamAttempt,
)
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


def _draft_exam(author, questions=()):
    now = timezone.now()
    exam = MasterExam.objects.create(
        name='Draft Exam',
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


class AddRemoveReorderAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod_user', role='moderator')
        self.q1 = make_question(owner=self.mod)
        self.q2 = make_question(owner=self.mod)
        self.q3 = make_question(owner=self.mod)
        self.exam = _draft_exam(self.mod, questions=[self.q1, self.q2])
        self.client.force_login(self.mod)

    def test_add_questions_appends(self):
        resp = self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/questions/add/',
            {'question_ids': [self.q3.id]},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.exam.exam_questions.count(), 3)

    def test_add_questions_dedupes(self):
        resp = self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/questions/add/',
            {'question_ids': [self.q1.id, self.q1.id]},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.exam.exam_questions.count(), 2)

    def test_add_after_publish_window_started_refused(self):
        now = timezone.now()
        self.exam.opens_at = now - timedelta(hours=1)
        self.exam.closes_at = now + timedelta(hours=1)
        self.exam.save()
        resp = self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/questions/add/',
            {'question_ids': [self.q3.id]},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_remove_question(self):
        resp = self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/questions/remove/',
            {'question_id': self.q1.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.exam.exam_questions.count(), 1)

    def test_reorder_permutation(self):
        resp = self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/questions/reorder/',
            {'question_ids': [self.q2.id, self.q1.id]},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        ordered = list(
            self.exam.exam_questions
            .order_by('order')
            .values_list('question_id', flat=True)
        )
        self.assertEqual(ordered, [self.q2.id, self.q1.id])

    def test_reorder_mismatch_returns_409(self):
        resp = self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/questions/reorder/',
            {'question_ids': [self.q3.id, self.q1.id]},
            format='json',
        )
        self.assertEqual(resp.status_code, 409)

    def test_non_author_moderator_refused(self):
        other = make_user('other_mod', role='moderator')
        self.client.force_login(other)
        resp = self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/questions/add/',
            {'question_ids': [self.q3.id]},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)


class DraftsLibraryAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod_user', role='moderator')
        self.member = make_user('member_a')
        self.client.force_login(self.mod)

    def _make_draft(self, owner, question='Draft question?'):
        return make_question(
            owner=owner, question=question,
            is_draft=True, draft_owner=owner,
        )

    def test_member_cannot_access_library(self):
        self.client.force_login(self.member)
        resp = self.client.get('/api/v1/exam/master/drafts/')
        self.assertEqual(resp.status_code, 403)

    def test_moderator_sees_own_drafts(self):
        self._make_draft(self.mod, 'My draft')
        self._make_draft(self.member, 'Their draft')

        resp = self.client.get('/api/v1/exam/master/drafts/')
        self.assertEqual(resp.status_code, 200)
        texts = [i['question'] for i in resp.json()['data']['items']]
        self.assertIn('My draft', texts)
        # Moderators do not hold `manage_any`, so other authors' drafts
        # are excluded.
        self.assertNotIn('Their draft', texts)

    def test_admin_sees_every_draft(self):
        admin = make_user('admin_a')  # not admin role, but...
        admin.role = 'admin'
        admin.save()
        self._make_draft(admin, 'Admin draft')
        self._make_draft(self.member, 'Member draft')

        self.client.force_login(admin)
        resp = self.client.get('/api/v1/exam/master/drafts/')
        texts = [i['question'] for i in resp.json()['data']['items']]
        self.assertIn('Admin draft', texts)
        self.assertIn('Member draft', texts)

    def test_search_filters(self):
        self._make_draft(self.mod, 'Alpha topic?')
        self._make_draft(self.mod, 'Beta topic?')

        resp = self.client.get('/api/v1/exam/master/drafts/?search=Alpha')
        texts = [i['question'] for i in resp.json()['data']['items']]
        self.assertIn('Alpha topic?', texts)
        self.assertNotIn('Beta topic?', texts)

    def test_usage_orphan_filter(self):
        used = self._make_draft(self.mod, 'Used draft?')
        self._make_draft(self.mod, 'Unused draft?')
        MasterExamQuestion.objects.create(
            master_exam=_draft_exam(self.mod),
            question=used,
            order=1,
        )

        resp = self.client.get('/api/v1/exam/master/drafts/?usage=orphan')
        texts = [i['question'] for i in resp.json()['data']['items']]
        self.assertIn('Unused draft?', texts)
        self.assertNotIn('Used draft?', texts)

    def test_usage_attached_filter(self):
        used = self._make_draft(self.mod, 'Used draft?')
        self._make_draft(self.mod, 'Unused draft?')
        MasterExamQuestion.objects.create(
            master_exam=_draft_exam(self.mod),
            question=used,
            order=1,
        )

        resp = self.client.get('/api/v1/exam/master/drafts/?usage=attached')
        texts = [i['question'] for i in resp.json()['data']['items']]
        self.assertIn('Used draft?', texts)
        self.assertNotIn('Unused draft?', texts)

    def test_draft_detail_get(self):
        draft = self._make_draft(self.mod, 'Inspect me?')
        resp = self.client.get(f'/api/v1/exam/master/drafts/{draft.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['question'], 'Inspect me?')

    def test_draft_detail_put_updates_content(self):
        draft = self._make_draft(self.mod, 'Original?')
        resp = self.client.put(
            f'/api/v1/exam/master/drafts/{draft.id}/',
            {
                'question': 'Updated?',
                'choices': ['A', 'B'],
                'correct_answer': 1,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(draft.question, 'Updated?')

    def test_draft_delete_removes_unattached(self):
        draft = self._make_draft(self.mod, 'Delete me?')
        resp = self.client.delete(f'/api/v1/exam/master/drafts/{draft.id}/')
        self.assertEqual(resp.status_code, 200)

    def test_draft_delete_blocked_when_attached_to_live_exam(self):
        draft = self._make_draft(self.mod, 'Attached?')
        exam = _draft_exam(self.mod, questions=[draft])
        resp = self.client.delete(f'/api/v1/exam/master/drafts/{draft.id}/')
        self.assertEqual(resp.status_code, 409)

    def test_draft_delete_from_cancelled_exam_succeeds(self):
        draft = self._make_draft(self.mod, 'Cancelled attach?')
        exam = _draft_exam(self.mod, questions=[draft])
        exam.stored_status = 'cancelled'
        exam.save()

        resp = self.client.delete(f'/api/v1/exam/master/drafts/{draft.id}/')
        self.assertEqual(resp.status_code, 200)


class AddDraftAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod_user', role='moderator')
        self.exam = _draft_exam(self.mod)
        self.client.force_login(self.mod)

    def test_add_draft_to_exam(self):
        resp = self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/drafts/',
            {
                'question': 'New inline draft?',
                'choices': ['A', 'B', 'C'],
                'correct_answer': 1,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(self.exam.exam_questions.count(), 1)
        # Draft carries the acting author as both author and owner.
        draft = self.exam.exam_questions.first().question
        self.assertEqual(draft.authored_by, self.mod)
        self.assertEqual(draft.owned_by, self.mod)
        self.assertTrue(draft.is_draft)

    def test_add_draft_after_window_started_refused(self):
        now = timezone.now()
        self.exam.opens_at = now - timedelta(hours=1)
        self.exam.closes_at = now + timedelta(hours=1)
        self.exam.save()
        resp = self.client.post(
            f'/api/v1/exam/master/{self.exam.id}/drafts/',
            {
                'question': 'Late draft?',
                'choices': ['A', 'B'],
                'correct_answer': 1,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)