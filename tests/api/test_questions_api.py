# tests/api/test_questions_api.py
from rest_framework.test import APIClient

from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question, make_category


class QuestionListAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = make_user('alice')
        self.alice_draft = make_question(
            owner=self.user, is_draft=True,
            draft_owner=self.user, question='alice draft',
        )
        self.public_q = make_question(
            owner=self.user, question='public question',
        )

    def test_anonymous_refused(self):
        resp = self.client.get('/api/v1/questions/')
        self.assertIn(resp.status_code, (401, 403))

    def test_member_sees_only_public(self):
        self.client.force_login(self.user)
        resp = self.client.get('/api/v1/questions/')
        self.assertEqual(resp.status_code, 200)
        ids = {item['id'] for item in resp.json()['data']['items']}
        self.assertIn(self.public_q.id, ids)
        self.assertNotIn(self.alice_draft.id, ids)

    def test_category_filter_via_query_param(self):
        """
        The filter must actually filter. The previous version of this
        test asserted only the HTTP status code — a view that ignored
        the `category` query parameter entirely would have passed.
        The two setUp questions have no category, so filtering by
        `cat.id` must return exactly the newly-created question.
        """
        cat = make_category('unique-cat')
        matched = make_question(owner=self.user, category=cat)
        self.client.force_login(self.user)
        resp = self.client.get(f'/api/v1/questions/?category={cat.id}')
        self.assertEqual(resp.status_code, 200)
        ids = {item['id'] for item in resp.json()['data']['items']}
        self.assertEqual(ids, {matched.id})

    def test_category_ids_array_filter(self):
        """
        Multi-select shape: `category_ids` is a comma-joined list in
        the query string. The service accepts both this shape and the
        legacy single-`category` param; see QuestionService's
        `category_ids takes precedence` rule.
        """
        cat_a = make_category('cat-a')
        cat_b = make_category('cat-b')
        cat_c = make_category('cat-c')
        q_a = make_question(owner=self.user, category=cat_a)
        q_b = make_question(owner=self.user, category=cat_b)
        make_question(owner=self.user, category=cat_c)

        self.client.force_login(self.user)
        resp = self.client.get(
            f'/api/v1/questions/?category_ids={cat_a.id},{cat_b.id}'
        )
        self.assertEqual(resp.status_code, 200)
        ids = {item['id'] for item in resp.json()['data']['items']}
        self.assertEqual(ids, {q_a.id, q_b.id})


class QuestionCreateAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = make_user('alice')
        self.client.force_login(self.user)

    def _payload(self, **kw):
        base = {
            'question': 'New question?',
            'choices': ['A', 'B', 'C'],
            'correct_answer': 1,
            'difficulty': 'medium',
        }
        base.update(kw)
        return base

    def test_member_can_create(self):
        resp = self.client.post(
            '/api/v1/questions/', self._payload(), format='json',
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()['data']
        self.assertEqual(body['question'], 'New question?')
        self.assertEqual(body['authored_by'], self.user.id)
        self.assertEqual(body['owned_by'], self.user.id)

    def test_single_choice_rejected(self):
        resp = self.client.post(
            '/api/v1/questions/',
            self._payload(choices=['only one']),
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_out_of_range_correct_answer_rejected(self):
        resp = self.client.post(
            '/api/v1/questions/',
            self._payload(correct_answer=99),
            format='json',
        )
        self.assertEqual(resp.status_code, 400)


class QuestionDetailAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.alice_q = make_question(owner=self.alice, question='alice?')

    def test_edit_requires_ownership(self):
        self.client.force_login(self.bob)
        resp = self.client.put(
            f'/api/v1/questions/{self.alice_q.id}/',
            {'question': 'hijack'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_owner_can_edit_own_question(self):
        self.client.force_login(self.alice)
        resp = self.client.put(
            f'/api/v1/questions/{self.alice_q.id}/',
            {'question': 'updated?'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)

    def test_moderator_can_edit_any(self):
        mod = make_user('mod1', role='moderator')
        self.client.force_login(mod)
        resp = self.client.put(
            f'/api/v1/questions/{self.alice_q.id}/',
            {'question': 'moderated?'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)

    def test_stale_version_returns_409(self):
        self.client.force_login(self.alice)
        self.alice_q.version = 5
        self.alice_q.save()
        resp = self.client.put(
            f'/api/v1/questions/{self.alice_q.id}/',
            {'question': 'conflict?', 'expected_version': 1},
            format='json',
        )
        self.assertEqual(resp.status_code, 409)

    def test_delete_own_succeeds(self):
        self.client.force_login(self.alice)
        resp = self.client.delete(f'/api/v1/questions/{self.alice_q.id}/')
        self.assertEqual(resp.status_code, 200)

    def test_delete_foreign_refused(self):
        self.client.force_login(self.bob)
        resp = self.client.delete(f'/api/v1/questions/{self.alice_q.id}/')
        self.assertEqual(resp.status_code, 403)


class BulkVerifyAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod1', role='moderator')
        self.alice = make_user('alice')
        self.q1 = make_question(owner=self.alice)
        self.q2 = make_question(owner=self.alice)

    def test_member_cannot_bulk_verify(self):
        self.client.force_login(self.alice)
        resp = self.client.post(
            '/api/v1/questions/bulk-verify/',
            {'question_ids': [self.q1.id, self.q2.id], 'action': 'verify'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_moderator_can_bulk_verify(self):
        self.client.force_login(self.mod)
        resp = self.client.post(
            '/api/v1/questions/bulk-verify/',
            {'question_ids': [self.q1.id, self.q2.id], 'action': 'verify'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.q1.refresh_from_db()
        self.q2.refresh_from_db()
        self.assertTrue(self.q1.verified)
        self.assertTrue(self.q2.verified)


class AvailableCountAPITests(CacheClearingTestCase):
    def test_count_matches_filter(self):
        user = make_user('alice')
        make_question(owner=user, difficulty='easy')
        make_question(owner=user, difficulty='hard')
        client = APIClient()
        client.force_login(user)
        resp = client.get('/api/v1/questions/available-count/?difficulty=easy')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['count'], 1)