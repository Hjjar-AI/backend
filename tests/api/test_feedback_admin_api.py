# tests/api/test_feedback_admin_api.py
from rest_framework.test import APIClient

from apps.feedback.models import QuestionFlag
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


class AdminFlagQueueAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod_user', role='moderator')
        self.member = make_user('member_a')
        self.author = make_user('author')
        self.q = make_question(owner=self.author, question='Reported?')
        self.client.force_login(self.mod)

    def test_moderator_can_access(self):
        resp = self.client.get('/api/v1/questions/admin/flags/')
        self.assertEqual(resp.status_code, 200)

    def test_member_refused(self):
        self.client.force_login(self.member)
        resp = self.client.get('/api/v1/questions/admin/flags/')
        self.assertEqual(resp.status_code, 403)

    def test_only_open_flags_listed(self):
        open_flag = QuestionFlag.objects.create(
            question=self.q, user=self.member, reason='open',
        )
        QuestionFlag.objects.create(
            question=self.q, user=self.author, reason='closed',
            resolved=True,
        )
        resp = self.client.get('/api/v1/questions/admin/flags/')
        ids = {item['id'] for item in resp.json()['data']['items']}
        self.assertIn(open_flag.id, ids)
        self.assertEqual(len(ids), 1)

    def test_question_author_reported_correctly(self):
        QuestionFlag.objects.create(
            question=self.q, user=self.member, reason='x',
        )
        resp = self.client.get('/api/v1/questions/admin/flags/')
        row = resp.json()['data']['items'][0]
        self.assertEqual(row['question_author'], 'author')
        self.assertEqual(row['flagger_username'], 'member_a')

    def test_flag_against_seed_question_reports_empty_author(self):
        seed_q = make_question(owner=None, authored_by=None)
        QuestionFlag.objects.create(
            question=seed_q, user=self.member, reason='x',
        )
        resp = self.client.get('/api/v1/questions/admin/flags/')
        row = next(
            r for r in resp.json()['data']['items']
            if r['question_id'] == seed_q.id
        )
        self.assertEqual(row['question_author'], '')


class AdminResolveFlagAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod_user', role='moderator')
        self.member = make_user('member_a')
        self.q = make_question(owner=self.mod)
        self.flag = QuestionFlag.objects.create(
            question=self.q, user=self.member, reason='x',
        )
        self.client.force_login(self.mod)

    def test_resolve_sets_fields(self):
        resp = self.client.post(
            f'/api/v1/questions/admin/flags/{self.flag.id}/resolve/'
        )
        self.assertEqual(resp.status_code, 200)
        self.flag.refresh_from_db()
        self.assertTrue(self.flag.resolved)
        self.assertEqual(self.flag.resolved_by, 'mod_user')
        self.assertIsNotNone(self.flag.resolved_at)

    def test_resolve_nonexistent_returns_404(self):
        resp = self.client.post('/api/v1/questions/admin/flags/99999/resolve/')
        self.assertEqual(resp.status_code, 404)

    def test_member_cannot_resolve(self):
        self.client.force_login(self.member)
        resp = self.client.post(
            f'/api/v1/questions/admin/flags/{self.flag.id}/resolve/'
        )
        self.assertEqual(resp.status_code, 403)

    def test_resolved_flag_allows_new_flag(self):
        """
        After a moderator closes a flag, the same user may open a new
        one. This is the partial-unique-index behavior end to end.
        """
        self.client.post(
            f'/api/v1/questions/admin/flags/{self.flag.id}/resolve/'
        )
        # User flags again via the feedback endpoint.
        self.client.force_login(self.member)
        resp = self.client.post(
            f'/api/v1/questions/{self.q.id}/flag/',
            {'reason': 'still wrong'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(
            QuestionFlag.objects.filter(
                question=self.q, user=self.member,
            ).count(),
            2,
        )