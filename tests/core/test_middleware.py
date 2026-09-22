# tests/core/test_middleware.py
from django.http import HttpResponse, HttpResponseRedirect
from django.test import RequestFactory

from apps.core.middleware import MustChangePasswordMiddleware
from tests.base import CacheClearingTestCase
from tests.factories import make_user


class MustChangePasswordMiddlewareTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.next = lambda req: HttpResponse('ok')

    def _run(self, path, user, method='get'):
        mw = MustChangePasswordMiddleware(self.next)
        req = getattr(self.factory, method)(path)
        req.user = user
        return mw(req)

    def test_anonymous_request_passes_through(self):
        from django.contrib.auth.models import AnonymousUser
        resp = self._run('/api/v1/questions/', AnonymousUser())
        self.assertEqual(resp.status_code, 200)

    def test_flag_off_passes_through(self):
        user = make_user(must_change_password=False)
        resp = self._run('/api/v1/questions/', user)
        self.assertEqual(resp.status_code, 200)

    def test_flag_on_blocks_api_with_403_json(self):
        user = make_user(must_change_password=True)
        resp = self._run('/api/v1/questions/', user)
        self.assertEqual(resp.status_code, 403)
        self.assertIn('application/json', resp['Content-Type'])

    def test_flag_on_redirects_admin_get(self):
        user = make_user(must_change_password=True)
        resp = self._run('/admin/users/user/', user, method='get')
        self.assertIsInstance(resp, HttpResponseRedirect)
        self.assertEqual(resp.url, '/admin/password_change/')

    def test_flag_on_returns_403_for_admin_post(self):
        """
        A 302 on a POST would be silently converted to a GET by the
        browser and lose the body. The middleware returns 403 instead.
        """
        user = make_user(must_change_password=True)
        resp = self._run('/admin/users/user/change/', user, method='post')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIsInstance(resp, HttpResponseRedirect)

    def test_change_password_endpoint_is_allowed(self):
        user = make_user(must_change_password=True)
        resp = self._run('/api/v1/auth/change-password/', user, method='post')
        self.assertEqual(resp.status_code, 200)

    def test_me_endpoint_is_allowed(self):
        user = make_user(must_change_password=True)
        resp = self._run('/api/v1/auth/me/', user)
        self.assertEqual(resp.status_code, 200)

    def test_admin_login_endpoint_is_allowed(self):
        user = make_user(must_change_password=True)
        resp = self._run('/admin/login/', user)
        self.assertEqual(resp.status_code, 200)

    def test_non_api_non_admin_path_is_unaffected(self):
        user = make_user(must_change_password=True)
        resp = self._run('/some/other/path/', user)
        self.assertEqual(resp.status_code, 200)