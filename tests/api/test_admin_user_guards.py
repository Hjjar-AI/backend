# tests/api/test_admin_user_guards.py
"""
The two admin-user guards that live on views other than
AdminUserDetailView:
  • AdminToggleUserView — same last-admin guard as the delete path
  • AdminResetPasswordView — self-reset keeps the acting session
    alive by calling update_session_auth_hash
"""
from rest_framework.test import APIClient

from apps.users.models import User
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_admin


class AdminToggleLastAdminGuardTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_toggle_last_admin_is_refused(self):
        """
        The toggle view has its own copy of the last-admin guard.
        A caller who holds `admin.users` but is not themselves an
        active admin, deactivating the only active admin, must be
        refused — the same as the delete path.
        """
        target = make_admin('target_admin', 'admin-pw-1234')

        actor = make_user(
            'override_actor',
            'actor-pw-1234',
            capabilities={'admin.users': True},
        )
        self.client.force_login(actor)

        resp = self.client.post(
            f'/api/v1/auth/admin/users/{target.id}/toggle/',
            {'admin_password': 'actor-pw-1234'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        target.refresh_from_db()
        self.assertTrue(target.is_active)

    def test_self_toggle_refused_even_when_another_admin_exists(self):
        """
        Self-toggle is rejected by AdminToggleUserView BEFORE the
        last-admin guard runs. This test pins that ordering: with
        two admins in the system, the actor can neither deactivate
        themselves nor reach the last-admin code path — the
        self-check fires first and returns 400.
        """
        admin_a = make_admin('admin_a', 'admin-pw-1234')
        make_admin('admin_b', 'admin-pw-1234')
        self.client.force_login(admin_a)
        resp = self.client.post(
            f'/api/v1/auth/admin/users/{admin_a.id}/toggle/',
            {'admin_password': 'admin-pw-1234'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

        # And the actor is untouched — the self-check is a hard
        # refusal, not a silent no-op with side effects.
        admin_a.refresh_from_db()
        self.assertTrue(admin_a.is_active)


class AdminResetSelfPasswordTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')

    def test_self_reset_keeps_session_alive(self):
        """
        When the acting user resets their own password, the view
        calls update_session_auth_hash so the session cookie stays
        valid. Without it, the next request would be a logout and
        the admin would have no way to know why.
        """
        self.client.force_login(self.admin)
        resp = self.client.post(
            f'/api/v1/auth/admin/users/{self.admin.id}/reset-password/',
            {'admin_password': 'admin-pw-1234'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)

        # The session is still valid.
        me = self.client.get('/api/v1/auth/me/')
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()['data']['username'], 'admin_a')

    def test_self_reset_returns_temp_password(self):
        self.client.force_login(self.admin)
        resp = self.client.post(
            f'/api/v1/auth/admin/users/{self.admin.id}/reset-password/',
            {'admin_password': 'admin-pw-1234'},
            format='json',
        )
        temp = resp.json()['data']['temp_password']
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password(temp))
        self.assertTrue(self.admin.must_change_password)

    def test_reset_another_user_does_not_affect_caller_session(self):
        other = make_user('other')
        self.client.force_login(self.admin)
        resp = self.client.post(
            f'/api/v1/auth/admin/users/{other.id}/reset-password/',
            {'admin_password': 'admin-pw-1234'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        me = self.client.get('/api/v1/auth/me/')
        self.assertEqual(me.status_code, 200)