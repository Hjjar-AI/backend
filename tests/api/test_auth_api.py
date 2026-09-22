# tests/api/test_auth_api.py
from rest_framework.test import APIClient

from tests.base import CacheClearingTestCase
from tests.factories import make_user


class LoginAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = make_user('alice', 'correct-horse-staple')

    def test_login_success_returns_user_payload(self):
        resp = self.client.post(
            '/api/v1/auth/login/',
            {'username': 'alice', 'password': 'correct-horse-staple'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['code'], 200)
        self.assertEqual(body['data']['user']['username'], 'alice')
        self.assertIn('capabilities', body['data']['user'])

    def test_login_wrong_password_returns_401(self):
        resp = self.client.post(
            '/api/v1/auth/login/',
            {'username': 'alice', 'password': 'wrong'},
            format='json',
        )
        self.assertEqual(resp.status_code, 401)

    def test_login_missing_fields_returns_400(self):
        resp = self.client.post('/api/v1/auth/login/', {}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_me_requires_authentication(self):
        resp = self.client.get('/api/v1/auth/me/')
        self.assertIn(resp.status_code, (401, 403))

    def test_me_returns_current_user(self):
        self.client.force_login(self.user)
        resp = self.client.get('/api/v1/auth/me/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['username'], 'alice')


class ChangePasswordAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = make_user('alice', 'old-password-1234')
        self.client.force_login(self.user)

    def test_change_password_success(self):
        resp = self.client.post(
            '/api/v1/auth/change-password/',
            {
                'current_password': 'old-password-1234',
                'new_password': 'NewStrong-Password-9876',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('NewStrong-Password-9876'))

    def test_wrong_current_password_returns_400(self):
        resp = self.client.post(
            '/api/v1/auth/change-password/',
            {
                'current_password': 'wrong',
                'new_password': 'NewStrong-Password-9876',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_weak_new_password_rejected(self):
        resp = self.client.post(
            '/api/v1/auth/change-password/',
            {'current_password': 'old-password-1234', 'new_password': '123'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)


class MustChangePasswordAPIGateTests(CacheClearingTestCase):
    """
    The middleware is unit-tested elsewhere. This exercises it through
    the URL resolver + full middleware stack — the wiring that can go
    wrong silently.
    """
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = make_user(
            'alice', 'pw-12345678', must_change_password=True,
        )
        self.client.force_login(self.user)

    def test_blocked_from_questions_list(self):
        resp = self.client.get('/api/v1/questions/')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            resp.json()['details']['reason'], 'MUST_CHANGE_PASSWORD',
        )

    def test_allowed_to_read_me(self):
        resp = self.client.get('/api/v1/auth/me/')
        self.assertEqual(resp.status_code, 200)

    def test_allowed_to_change_password(self):
        resp = self.client.post(
            '/api/v1/auth/change-password/',
            {
                'current_password': 'pw-12345678',
                'new_password': 'NewStrong-Password-123',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.must_change_password)


class CSRFEndpointTests(CacheClearingTestCase):
    def test_get_csrf_returns_token_and_cookie(self):
        resp = APIClient().get('/api/v1/auth/csrf/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('token', resp.json()['data'])
        self.assertIn('csrftoken', resp.cookies)