# tests/users/test_authentication.py
from datetime import timedelta
from unittest.mock import patch

from django.test import RequestFactory
from django.utils import timezone

from apps.users.services.authentication_service import AuthenticationService
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_admin, make_stub


class _FakeSession:
    def __init__(self):
        self.expiry = 'unset'

    def set_expiry(self, value):
        self.expiry = value


class LoginServiceTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        # Usernames here and below are alphanumeric — the model's
        # USERNAME_REGEX rejects hyphens.
        self.user = make_user('alice', 'correct-horse-staple')

    def _request(self):
        req = self.factory.post('/api/v1/auth/login/')
        req.META['REMOTE_ADDR'] = '127.0.0.1'
        req.session = _FakeSession()
        return req

    @patch('apps.users.services.authentication_service.login')
    def test_successful_login_returns_user(self, mock_login):
        u = AuthenticationService.login_user(
            'alice', 'correct-horse-staple', self._request(),
        )
        self.assertEqual(u, self.user)
        mock_login.assert_called_once()

    def test_wrong_password_returns_none(self):
        u = AuthenticationService.login_user(
            'alice', 'wrong-password', self._request(),
        )
        self.assertIsNone(u)

    @patch('apps.users.services.authentication_service.login')
    def test_lockout_after_five_failed_attempts(self, _login):
        for _ in range(5):
            AuthenticationService.login_user(
                'alice', 'wrong-password', self._request(),
            )
        u = AuthenticationService.login_user(
            'alice', 'correct-horse-staple', self._request(),
        )
        self.assertIsNone(u)

    @patch('apps.users.services.authentication_service.login')
    def test_successful_login_clears_failure_history(self, _login):
        for _ in range(3):
            AuthenticationService.login_user(
                'alice', 'wrong-password', self._request(),
            )
        AuthenticationService.login_user(
            'alice', 'correct-horse-staple', self._request(),
        )
        # 3 more failures should NOT tip the lockout.
        for _ in range(3):
            AuthenticationService.login_user(
                'alice', 'wrong-password', self._request(),
            )
        u = AuthenticationService.login_user(
            'alice', 'correct-horse-staple', self._request(),
        )
        self.assertIsNotNone(u)

    @patch('apps.users.services.authentication_service.login')
    def test_expired_user_without_auto_renew_is_refused(self, _login):
        self.user.expires_at = timezone.now() - timedelta(days=1)
        self.user.auto_renew_days = 0
        self.user.save()
        u = AuthenticationService.login_user(
            'alice', 'correct-horse-staple', self._request(),
        )
        self.assertIsNone(u)

    @patch('apps.users.services.authentication_service.login')
    def test_expired_user_with_auto_renew_is_renewed(self, _login):
        self.user.expires_at = timezone.now() - timedelta(days=1)
        self.user.auto_renew_days = 30
        self.user.save()
        u = AuthenticationService.login_user(
            'alice', 'correct-horse-staple', self._request(),
        )
        self.assertEqual(u, self.user)
        self.user.refresh_from_db()
        self.assertGreater(self.user.expires_at, timezone.now())

    @patch('apps.users.services.authentication_service.login')
    def test_admin_with_bypass_expiry_can_log_in_despite_expires_at(self, _login):
        admin = make_admin('the_admin', 'admin-pw-1234')
        admin.expires_at = timezone.now() - timedelta(days=30)
        admin.auto_renew_days = 0
        admin.save()
        u = AuthenticationService.login_user(
            'the_admin', 'admin-pw-1234', self._request(),
        )
        self.assertEqual(u, admin)

    def test_stub_cannot_authenticate(self):
        """
        Stubs are created with is_active=False and an unusable password.
        Django's ModelBackend.user_can_authenticate refuses them before
        any password comparison.
        """
        make_stub('external_author')
        u = AuthenticationService.login_user(
            'external_author', '', self._request(),
        )
        self.assertIsNone(u)

    @patch('apps.users.services.authentication_service.login')
    def test_session_lifetime_per_role(self, _login):
        # Usernames must be >= 3 chars.
        make_admin('adm2', 'admin-pw-1234')
        req = self._request()
        AuthenticationService.login_user('adm2', 'admin-pw-1234', req)
        self.assertEqual(req.session.expiry, 8 * 60 * 60)

        make_user('mod1', 'member-pw-1234', role='moderator')
        req = self._request()
        AuthenticationService.login_user('mod1', 'member-pw-1234', req)
        self.assertEqual(req.session.expiry, 7 * 24 * 60 * 60)

        make_user('mem1', 'member-pw-1234')
        req = self._request()
        AuthenticationService.login_user('mem1', 'member-pw-1234', req)
        self.assertIsNone(req.session.expiry)  # → SESSION_COOKIE_AGE