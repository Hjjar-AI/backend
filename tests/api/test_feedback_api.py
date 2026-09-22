# tests/api/test_feedback_api.py
from rest_framework.test import APIClient

from apps.feedback.models import Bookmark, QuestionFlag
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


class FeedbackVisibilityGateTests(CacheClearingTestCase):
    """
    The visibility gate in apps/feedback/views.py fixes a real
    enumeration oracle: before the fix, any authenticated user could
    POST /bookmark/ with an arbitrary question id, read the
    404-vs-200 difference to enumerate other authors' drafts, and
    then read the content via /bookmarks/.
    """
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.bob_draft = make_question(
            owner=self.bob, is_draft=True,
            draft_owner=self.bob, question='bob private draft',
        )
        self.client.force_login(self.alice)

    def test_bookmark_invisible_draft_returns_404(self):
        resp = self.client.post(
            f'/api/v1/questions/{self.bob_draft.id}/bookmark/',
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Bookmark.objects.count(), 0)

    def test_flag_invisible_draft_returns_404(self):
        resp = self.client.post(
            f'/api/v1/questions/{self.bob_draft.id}/flag/',
            {'reason': 'probe'},
            format='json',
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(QuestionFlag.objects.count(), 0)

    def test_rate_invisible_draft_returns_404(self):
        resp = self.client.post(
            f'/api/v1/questions/{self.bob_draft.id}/rate/',
            {'rating': 5},
            format='json',
        )
        self.assertEqual(resp.status_code, 404)

    def test_nonexistent_question_returns_404(self):
        resp = self.client.post('/api/v1/questions/999999/bookmark/')
        self.assertEqual(resp.status_code, 404)

    def test_bookmark_visible_public_question_succeeds(self):
        public = make_question(owner=self.bob, question='public')
        resp = self.client.post(
            f'/api/v1/questions/{public.id}/bookmark/',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Bookmark.objects.count(), 1)


class BatchRatingsAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = make_user('alice')
        self.client.force_login(self.user)
        self.q1 = make_question(owner=self.user)
        self.q2 = make_question(owner=self.user)

    def test_empty_ids_returns_empty_list(self):
        resp = self.client.get('/api/v1/questions/ratings/?ids=')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['items'], [])

    def test_returns_one_row_per_visible_id(self):
        resp = self.client.get(
            f'/api/v1/questions/ratings/?ids={self.q1.id},{self.q2.id}'
        )
        self.assertEqual(resp.status_code, 200)
        items = resp.json()['data']['items']
        self.assertEqual(len(items), 2)
        ids = {item['question_id'] for item in items}
        self.assertEqual(ids, {self.q1.id, self.q2.id})

    def test_too_many_ids_rejected(self):
        ids = ','.join(str(i) for i in range(600))
        resp = self.client.get(f'/api/v1/questions/ratings/?ids={ids}')
        self.assertEqual(resp.status_code, 400)