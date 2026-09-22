# tests/core/test_csrf_enforcement.py
"""
CSRF enforcement end-to-end through the DRF session-auth layer.

Django's test client defaults to `enforce_csrf_checks=False`, which
sets `request._dont_enforce_csrf_checks = True`. DRF's
SessionAuthentication reads that flag and skips its own CSRF check.
Every POST in the current suite silently bypasses CSRF as a result.

These tests construct the client with `enforce_csrf_checks=True` so
DRF's CSRF check actually runs. They cover the three meaningful
cases:
  • session present, no CSRF cookie       → 403
  • session present, CSRF cookie, no header → 403
  • session present, CSRF cookie + header → 201

The login endpoint itself is exempt — DRF only enforces CSRF for
authenticated requests. That is documented by the fourth test.
"""
from rest_framework.test import APIClient

from apps.questions.models import Question
from tests.base import CacheClearingTestCase
from tests.factories import make_user


class CSRFEnforcementTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('alice')
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.user)

    def _question_payload(self):
        return {
            'question': 'CSRF test?',
            'choices': ['A', 'B'],
            'correct_answer': 1,
        }

    def test_post_without_csrf_cookie_is_rejected(self):
        """
        Session present, no CSRF cookie at all. DRF's CSRF check
        raises PermissionDenied before the view body runs.
        """
        resp = self.client.post(
            '/api/v1/questions/', self._question_payload(), format='json',
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Question.objects.count(), 0)

    def test_post_with_cookie_but_no_header_is_rejected(self):
        """
        The classic CSRF scenario: a cross-origin page can trigger
        the browser to send the session cookie, but cannot read it
        and therefore cannot set the matching header. The request
        must fail.
        """
        # Prime the CSRF cookie.
        self.client.get('/api/v1/auth/csrf/')

        resp = self.client.post(
            '/api/v1/questions/', self._question_payload(), format='json',
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Question.objects.count(), 0)

    def test_post_with_valid_csrf_token_succeeds(self):
        csrf_resp = self.client.get('/api/v1/auth/csrf/')
        token = csrf_resp.json()['data']['token']

        resp = self.client.post(
            '/api/v1/questions/',
            self._question_payload(),
            format='json',
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(Question.objects.count(), 1)

    def test_login_endpoint_is_csrf_exempt(self):
        """
        Login runs before the user has a session, so DRF's session
        auth has nothing to check against. Without this exemption,
        a user could never log in.
        """
        anon_client = APIClient(enforce_csrf_checks=True)
        resp = anon_client.post(
            '/api/v1/auth/login/',
            {'username': 'alice', 'password': 'wrong-pw'},
            format='json',
        )
        # 401, not 403 — the request reached the view.
        self.assertEqual(resp.status_code, 401)


class LogoutCSRFEnforcementTests(CacheClearingTestCase):
    """
    Logout mutates server-side state (deletes the session row), so
    it must be CSRF-protected. Same contract as the question POST.
    """
    def setUp(self):
        super().setUp()
        self.user = make_user('alice')
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.user)

    def test_logout_without_csrf_is_rejected(self):
        self.client.get('/api/v1/auth/csrf/')
        resp = self.client.post('/api/v1/auth/logout/')
        self.assertEqual(resp.status_code, 403)

    def test_logout_with_valid_csrf_succeeds(self):
        csrf_resp = self.client.get('/api/v1/auth/csrf/')
        token = csrf_resp.json()['data']['token']
        resp = self.client.post(
            '/api/v1/auth/logout/',
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(resp.status_code, 200)