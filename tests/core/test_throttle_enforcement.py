# tests/core/test_throttle_enforcement.py
"""
Throttle enforcement. The suite up to now never hit a 429 — the
only evidence any throttle runs was an incidental log line. A typo
in `REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']` was invisible.

Each test class clears the cache in setUp, so the throttle counter
starts at zero. Requests are made in a tight loop; the throttle
state lives entirely in the locmem cache, which is per-process.
"""
from rest_framework.test import APIClient

from tests.base import CacheClearingTestCase
from tests.factories import make_user


class LoginCredentialsThrottleTests(CacheClearingTestCase):
    """
    LoginCredentialsRateThrottle: 5/min keyed on (IP, username).
    """
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        make_user('alice', 'correct-pw-1234')
        make_user('bob', 'correct-pw-1234')

    def _login(self, username='alice', password='wrong-pw'):
        return self.client.post(
            '/api/v1/auth/login/',
            {'username': username, 'password': password},
            format='json',
        )

    def test_sixth_request_for_same_user_is_throttled(self):
        for _ in range(5):
            resp = self._login()
            self.assertEqual(resp.status_code, 401)

        resp = self._login()
        self.assertEqual(resp.status_code, 429)

    def test_throttle_key_is_per_username(self):
        for _ in range(5):
            self._login(username='alice')

        # Different username → different cache key → not throttled.
        resp = self._login(username='bob')
        self.assertEqual(resp.status_code, 401)

    def test_throttle_does_not_block_anonymous_other_endpoints(self):
        """
        The throttle is scoped to the login view only. Five failed
        logins must not throttle /api/v1/auth/csrf/.
        """
        for _ in range(5):
            self._login()
        resp = self.client.get('/api/v1/auth/csrf/')
        self.assertEqual(resp.status_code, 200)


class ImportThrottleTests(CacheClearingTestCase):
    """
    ImportRateThrottle: 10/hour. Applied to the three import views
    in apps/database/views.py.
    """
    def setUp(self):
        super().setUp()
        from tests.factories import make_admin
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)

    def test_eleventh_import_request_is_throttled(self):
        """
        Each request counts against the throttle regardless of the
        outcome. All ten miss the file and return 400; the eleventh
        is blocked before the view runs.
        """
        for _ in range(10):
            resp = self.client.post('/api/v1/database/import/')
            self.assertEqual(resp.status_code, 400)

        resp = self.client.post('/api/v1/database/import/')
        self.assertEqual(resp.status_code, 429)