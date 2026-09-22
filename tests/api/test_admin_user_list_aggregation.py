# tests/api/test_admin_user_list_aggregation.py
"""
The batched latest-exam columns on AdminUserListView.

The list response carries `latest_exam_accuracy` and
`latest_exam_tag` for each user. The view computes them with two
queries for the whole page (a MAX per user plus one OR query) —
NOT a correlated subquery per row. These tests verify the output
shape and that the correct row is picked when a user has history.
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from tests.base import CacheClearingTestCase
from tests.factories import make_admin, make_user, make_test_history


class AdminUserListLatestExamTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)

    def test_user_with_no_history_has_null_columns(self):
        make_user('quiet_user')
        resp = self.client.get('/api/v1/auth/admin/users/')
        row = next(
            i for i in resp.json()['data']['items']
            if i['username'] == 'quiet_user'
        )
        self.assertIsNone(row['latest_exam_accuracy'])
        self.assertIsNone(row['latest_exam_tag'])

    def test_user_with_one_session_shows_values(self):
        u = make_user('active_user')
        make_test_history(u, accuracy=87.5, tag='cardiology')
        resp = self.client.get('/api/v1/auth/admin/users/')
        row = next(
            i for i in resp.json()['data']['items']
            if i['username'] == 'active_user'
        )
        self.assertEqual(row['latest_exam_accuracy'], 87.5)
        self.assertEqual(row['latest_exam_tag'], 'cardiology')

    def test_latest_session_wins(self):
        u = make_user('history_user')
        now = timezone.now()
        make_test_history(
            u, accuracy=50.0, tag='old',
            completed_at=now - timedelta(days=5),
        )
        make_test_history(
            u, accuracy=95.0, tag='new', completed_at=now,
        )
        resp = self.client.get('/api/v1/auth/admin/users/')
        row = next(
            i for i in resp.json()['data']['items']
            if i['username'] == 'history_user'
        )
        self.assertEqual(row['latest_exam_accuracy'], 95.0)
        self.assertEqual(row['latest_exam_tag'], 'new')

    def test_accuracy_is_rounded_to_one_decimal(self):
        u = make_user('rounding_user')
        make_test_history(u, accuracy=87.5555)
        resp = self.client.get('/api/v1/auth/admin/users/')
        row = next(
            i for i in resp.json()['data']['items']
            if i['username'] == 'rounding_user'
        )
        self.assertEqual(row['latest_exam_accuracy'], 87.6)

    def test_stub_users_are_excluded_from_list(self):
        from tests.factories import make_stub
        make_stub('stub_author')
        resp = self.client.get('/api/v1/auth/admin/users/')
        usernames = {i['username'] for i in resp.json()['data']['items']}
        self.assertNotIn('stub_author', usernames)


class AdminUserDetailLatestExamTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)

    def test_detail_includes_latest_exam_columns(self):
        u = make_user('detail_user')
        make_test_history(u, accuracy=72.0, tag='neurology')
        resp = self.client.get(f'/api/v1/auth/admin/users/{u.id}/')
        data = resp.json()['data']
        self.assertEqual(data['latest_exam_accuracy'], 72.0)
        self.assertEqual(data['latest_exam_tag'], 'neurology')

    def test_detail_with_no_history_has_nulls(self):
        u = make_user('detail_quiet')
        resp = self.client.get(f'/api/v1/auth/admin/users/{u.id}/')
        data = resp.json()['data']
        self.assertIsNone(data['latest_exam_accuracy'])
        self.assertIsNone(data['latest_exam_tag'])

    def test_expiry_days_left_reported_for_non_admin(self):
        u = make_user(
            'expiring',
            expires_at=timezone.now() + timedelta(days=15),
            auto_renew_days=0,
        )
        resp = self.client.get(f'/api/v1/auth/admin/users/{u.id}/')
        data = resp.json()['data']
        # Somewhere between 14 and 15 days depending on microsecond
        # timing. Assert the range rather than the exact value.
        self.assertIn(data['expiry_days_left'], (14, 15))

    def test_expiry_days_left_is_null_for_admin(self):
        """An admin's expiry is bypassed, so the UI should not show a countdown."""
        other = make_admin('admin_b', 'admin-pw-1234')
        other.expires_at = timezone.now() + timedelta(days=10)
        other.save()
        resp = self.client.get(f'/api/v1/auth/admin/users/{other.id}/')
        self.assertIsNone(resp.json()['data']['expiry_days_left'])

    def test_expiry_days_left_null_when_no_expires_at(self):
        u = make_user('no_expiry')
        resp = self.client.get(f'/api/v1/auth/admin/users/{u.id}/')
        self.assertIsNone(resp.json()['data']['expiry_days_left'])