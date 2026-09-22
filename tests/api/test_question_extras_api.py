# tests/api/test_question_extras_api.py
from rest_framework.test import APIClient

from apps.questions.models import Question
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


class QuestionBatchAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_hydrates_ids(self):
        q1 = make_question(owner=self.u)
        q2 = make_question(owner=self.u)
        resp = self.client.post(
            '/api/v1/questions/batch/',
            {'ids': [q1.id, q2.id]},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(data['count'], 2)
        ids = {item['id'] for item in data['items']}
        self.assertEqual(ids, {q1.id, q2.id})

    def test_empty_ids_returns_empty(self):
        resp = self.client.post(
            '/api/v1/questions/batch/', {'ids': []}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['items'], [])

    def test_over_500_ids_rejected(self):
        ids = list(range(1, 502))
        resp = self.client.post(
            '/api/v1/questions/batch/', {'ids': ids}, format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_invisible_draft_is_dropped(self):
        other = make_user('bob')
        bob_draft = make_question(
            owner=other, is_draft=True,
            draft_owner=other, question='bob draft',
        )
        resp = self.client.post(
            '/api/v1/questions/batch/',
            {'ids': [bob_draft.id]},
            format='json',
        )
        self.assertEqual(resp.json()['data']['count'], 0)


class QuestionDuplicateAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_duplicate_public_question_creates_public_copy(self):
        original = make_question(owner=self.u, question='Original?')
        resp = self.client.post(
            f'/api/v1/questions/{original.id}/duplicate/'
        )
        self.assertEqual(resp.status_code, 201)
        new_id = resp.json()['data']['id']
        self.assertNotEqual(new_id, original.id)
        copy = Question.objects.get(id=new_id)
        self.assertFalse(copy.is_draft)
        self.assertEqual(copy.authored_by, self.u)

    def test_duplicate_own_draft_creates_draft_copy(self):
        draft = make_question(
            owner=self.u, is_draft=True, draft_owner=self.u,
            question='Draft?',
        )
        resp = self.client.post(
            f'/api/v1/questions/{draft.id}/duplicate/'
        )
        self.assertEqual(resp.status_code, 201)
        copy = Question.objects.get(id=resp.json()['data']['id'])
        self.assertTrue(copy.is_draft)
        self.assertEqual(copy.draft_owner, self.u)

    def test_duplicate_invisible_draft_returns_404(self):
        other = make_user('bob')
        bob_draft = make_question(
            owner=other, is_draft=True, draft_owner=other,
        )
        resp = self.client.post(
            f'/api/v1/questions/{bob_draft.id}/duplicate/'
        )
        self.assertEqual(resp.status_code, 404)

    def test_duplicate_copies_tags(self):
        from tests.factories import make_tag
        tag = make_tag('copied-tag')
        original = make_question(owner=self.u)
        original.tags.add(tag)

        resp = self.client.post(
            f'/api/v1/questions/{original.id}/duplicate/'
        )
        copy = Question.objects.get(id=resp.json()['data']['id'])
        self.assertIn('copied-tag', list(copy.tags.values_list('name', flat=True)))