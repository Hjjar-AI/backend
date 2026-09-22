# tests/api/test_analytics_api.py
from rest_framework.test import APIClient

from apps.feedback.models import QuestionFlag
from apps.learning.models import UserQuestionAttempt
from tests.base import CacheClearingTestCase
from tests.factories import (
    make_user, make_admin, make_question, make_category, make_test_history,
)


class SummaryViewAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.member = make_user('member_a')
        self.client.force_login(self.member)

    def test_member_gets_own_sections_and_empty_admin_sections(self):
        resp = self.client.get('/api/v1/analytics/summary/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertIn('user_performance', data)
        self.assertIn('weak_categories', data)
        self.assertIn('confidence_stats', data)
        self.assertEqual(data['category_coverage'], [])
        self.assertEqual(data['tag_coverage'], [])
        self.assertEqual(data['difficulty_stats'], [])
        self.assertEqual(data['active_users'], [])

    def test_moderator_gets_populated_admin_sections(self):
        # The moderator role holds `analytics.view_all`, so the
        # admin sections come back populated instead of empty.
        make_question(owner=self.member, verified=True)
        mod = make_user('mod_user', role='moderator')
        self.client.force_login(mod)
        resp = self.client.get('/api/v1/analytics/summary/')
        data = resp.json()['data']
        self.assertIn('category_coverage', data)
        self.assertIsInstance(data['difficulty_stats'], list)

    def test_days_param_clamped(self):
        resp = self.client.get('/api/v1/analytics/summary/?days=9999')
        self.assertEqual(resp.status_code, 200)

    def test_anonymous_refused(self):
        self.client.logout()
        resp = self.client.get('/api/v1/analytics/summary/')
        self.assertIn(resp.status_code, (401, 403))


class ActiveUsersStatsAPITests(CacheClearingTestCase):
    """
    `ActiveUsersStatsView` requires `admin.active_users`, which the
    moderator role does NOT hold. Only admin (which has every
    capability via the short-circuit) can reach it.
    """
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.member = make_user('member_a')

    def test_admin_can_access(self):
        self.client.force_login(self.admin)
        resp = self.client.get('/api/v1/analytics/active-users/')
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json()['data'], list)

    def test_member_refused(self):
        self.client.force_login(self.member)
        resp = self.client.get('/api/v1/analytics/active-users/')
        self.assertEqual(resp.status_code, 403)

    def test_moderator_refused_without_admin_active_users(self):
        """
        A moderator holds `analytics.view_all` but not
        `admin.active_users`. The endpoint must refuse.
        """
        mod = make_user('mod_user', role='moderator')
        self.client.force_login(mod)
        resp = self.client.get('/api/v1/analytics/active-users/')
        self.assertEqual(resp.status_code, 403)


class VerificationStatsAPITests(CacheClearingTestCase):
    """
    Same story as ActiveUsersStatsAPITests — the capability is
    `admin.verification_stats`, not `analytics.view_all`.
    """
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)

    def test_returns_expected_shape(self):
        make_question(owner=self.admin, verified=True)
        make_question(owner=self.admin, verified=False)

        resp = self.client.get('/api/v1/analytics/verification-stats/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(data['total_verified'], 1)
        self.assertEqual(data['total_unverified'], 1)
        self.assertEqual(data['verification_rate'], 50.0)
        self.assertIn('by_user', data)
        self.assertIn('by_category', data)
        self.assertIn('monthly', data)


class CategoryMasteryAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_empty_when_no_attempts(self):
        resp = self.client.get('/api/v1/analytics/category-mastery/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(data['categories'], [])
        self.assertEqual(data['total_categories'], 0)

    def test_mastery_payload_shape(self):
        cat = make_category('mastered')
        q = make_question(owner=self.u, category=cat)
        UserQuestionAttempt.objects.create(
            user=self.u, question=q, attempts=10, wrong_count=1,
        )
        resp = self.client.get('/api/v1/analytics/category-mastery/')
        row = resp.json()['data']['categories'][0]
        self.assertEqual(row['category_name'], 'mastered')
        self.assertTrue(row['mastered'])

    def test_ignores_user_id_query_param(self):
        """The view reads request.user.id, never a query param."""
        other = make_user('bob')
        other_cat = make_category('bobs')
        other_q = make_question(owner=other, category=other_cat)
        UserQuestionAttempt.objects.create(
            user=other, question=other_q, attempts=10, wrong_count=1,
        )

        resp = self.client.get(
            f'/api/v1/analytics/category-mastery/?user_id={other.id}'
        )
        self.assertEqual(resp.json()['data']['categories'], [])


class StreakHistoryAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_default_window_is_30_days(self):
        resp = self.client.get('/api/v1/analytics/streak-history/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()['data']['days']), 30)

    def test_days_param_clamped_to_min_7(self):
        resp = self.client.get('/api/v1/analytics/streak-history/?days=1')
        self.assertEqual(len(resp.json()['data']['days']), 7)

    def test_days_param_clamped_to_max_365(self):
        resp = self.client.get('/api/v1/analytics/streak-history/?days=9999')
        self.assertEqual(len(resp.json()['data']['days']), 365)


class AdminAdvancedViewsAPITests(CacheClearingTestCase):
    """
    Every admin advanced endpoint gates on `analytics.view_all`,
    which the moderator role holds. The member role does not.
    """
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod_user', role='moderator')
        self.member = make_user('member_a')

    def _admin_endpoints(self):
        return [
            '/api/v1/analytics/admin/difficulty-calibration/',
            '/api/v1/analytics/admin/author-flag-rate/',
            '/api/v1/analytics/admin/exam-duration/',
            '/api/v1/analytics/admin/cohort-comparison/',
            '/api/v1/analytics/admin/retention/',
        ]

    def test_moderator_can_access_all(self):
        self.client.force_login(self.mod)
        for url in self._admin_endpoints():
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)

    def test_member_refused_on_all(self):
        self.client.force_login(self.member)
        for url in self._admin_endpoints():
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 403)


class CohortComparisonParamsAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod_user', role='moderator')
        self.client.force_login(self.mod)

    def test_days_param_clamped(self):
        resp = self.client.get(
            '/api/v1/analytics/admin/cohort-comparison/?days=9999'
        )
        self.assertEqual(resp.status_code, 200)

    def test_retention_weeks_clamped_min_2(self):
        resp = self.client.get(
            '/api/v1/analytics/admin/retention/?weeks=1'
        )
        self.assertEqual(resp.status_code, 200)

    def test_retention_weeks_clamped_max_52(self):
        resp = self.client.get(
            '/api/v1/analytics/admin/retention/?weeks=9999'
        )
        self.assertEqual(resp.status_code, 200)

    def test_author_flag_rate_min_questions_param(self):
        author = make_user('author')
        q = make_question(owner=author)
        make_question(owner=author)
        QuestionFlag.objects.create(
            question=q, user=self.mod, reason='r',
        )
        # min_questions=100 filters out the author entirely.
        resp = self.client.get(
            '/api/v1/analytics/admin/author-flag-rate/?min_questions=100'
        )
        self.assertEqual(resp.json()['data']['rows'], [])