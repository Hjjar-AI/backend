# tests/core/test_middleware_extra.py
"""
The other two middlewares. MustChangePasswordMiddleware has its
own file; AutoRenewMiddleware and ActiveSessionMiddleware live here.
"""
import time
from datetime import timedelta

from django.http import HttpResponse
from django.test import RequestFactory
from django.utils import timezone

from apps.core.middleware import (
    AutoRenewMiddleware, ActiveSessionMiddleware,
)
from apps.users.models import ActiveSession
from tests.base import CacheClearingTestCase
from tests.factories import make_user


class _FakeSession(dict):
    """Minimal session stub with a `modified` attribute."""
    def __init__(self):
        super().__init__()
        self.modified = False

    def save(self):
        pass

    @property
    def session_key(self):
        return self.get('_key', 'test-key-1')


class AutoRenewMiddlewareTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.mw = AutoRenewMiddleware(lambda r: HttpResponse('ok'))

    def test_anonymous_passes_through(self):
        from django.contrib.auth.models import AnonymousUser
        req = self.factory.get('/')
        req.user = AnonymousUser()
        resp = self.mw(req)
        self.assertEqual(resp.status_code, 200)

    def test_user_not_due_for_renewal_untouched(self):
        user = make_user(
            'alice',
            expires_at=timezone.now() + timedelta(days=10),
            auto_renew_days=30,
        )
        original = user.expires_at
        req = self.factory.get('/')
        req.user = user
        self.mw(req)
        user.refresh_from_db()
        self.assertEqual(user.expires_at, original)

    def test_user_due_for_renewal_gets_extended(self):
        user = make_user(
            'alice',
            expires_at=timezone.now() - timedelta(days=1),
            auto_renew_days=30,
        )
        req = self.factory.get('/')
        req.user = user
        self.mw(req)
        user.refresh_from_db()
        self.assertGreater(user.expires_at, timezone.now())


class ActiveSessionMiddlewareTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.mw = ActiveSessionMiddleware(lambda r: HttpResponse('ok'))
        self.user = make_user('alice')

    def _request(self):
        req = self.factory.get('/')
        req.user = self.user
        req.session = _FakeSession()
        req.META['REMOTE_ADDR'] = '127.0.0.1'
        req.META['HTTP_USER_AGENT'] = 'test-agent'
        return req

    def test_anonymous_request_does_not_create_session_row(self):
        from django.contrib.auth.models import AnonymousUser
        req = self._request()
        req.user = AnonymousUser()
        self.mw(req)
        self.assertEqual(ActiveSession.objects.count(), 0)

    def test_authenticated_request_creates_session_row(self):
        self.mw(self._request())
        self.assertEqual(ActiveSession.objects.count(), 1)
        row = ActiveSession.objects.first()
        self.assertEqual(row.user, self.user)
        self.assertEqual(row.ip, '127.0.0.1')
        self.assertEqual(row.user_agent, 'test-agent')

    def test_heartbeat_throttles_repeated_updates(self):
        # First call creates the row and sets `_last_tracked`.
        req1 = self._request()
        self.mw(req1)
        self.assertEqual(ActiveSession.objects.count(), 1)

        # Second call within the heartbeat window should not update
        # (the timestamp on the row stays the same).
        row = ActiveSession.objects.first()
        original_seen = row.last_seen

        # Simulate the session carrying the heartbeat marker.
        req2 = self._request()
        req2.session['_last_tracked'] = time.time()
        self.mw(req2)

        row.refresh_from_db()
        self.assertEqual(row.last_seen, original_seen)

    def test_active_session_error_does_not_fail_request(self):
        """
        A failure to write the presence row must not fail the
        request that triggered it. The middleware swallows the
        exception and logs it.
        """
        from unittest.mock import patch
        from django.db import DatabaseError

        with patch.object(
            ActiveSession.objects, 'update_or_create',
            side_effect=DatabaseError('simulated'),
        ):
            resp = self.mw(self._request())
        self.assertEqual(resp.status_code, 200)