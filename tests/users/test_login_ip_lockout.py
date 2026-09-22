# tests/users/test_login_ip_lockout.py
"""
The IP-wide half of LoginSecurityService.

The username-scoped lockout (5 failed attempts per IP+username)
is tested in `test_authentication.py`. The IP-wide ceiling (30
failed attempts per IP, regardless of username) is a separate
mechanism and is not exercised anywhere. It is the defense against
a distributed spray targeting many usernames from one source.
"""
from apps.users.services.login_security_service import LoginSecurityService
from tests.base import CacheClearingTestCase


class LoginIpLockoutTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.svc = LoginSecurityService()
        self.ip = '1.2.3.4'

    def test_ip_wide_limit_fires_after_threshold(self):
        for i in range(30):
            self.svc.record_attempt(self.ip, f'user_{i}', success=False)
        self.assertTrue(self.svc.is_blocked(self.ip, 'fresh_user'))

    def test_under_threshold_does_not_fire(self):
        for i in range(29):
            self.svc.record_attempt(self.ip, f'user_{i}', success=False)
        self.assertFalse(self.svc.is_blocked(self.ip, 'fresh_user'))

    def test_successful_attempts_do_not_count_toward_ip_limit(self):
        for _ in range(30):
            self.svc.record_attempt(self.ip, 'lucky_user', success=True)
        self.assertFalse(self.svc.is_blocked(self.ip, 'fresh_user'))

    def test_other_ip_is_unaffected(self):
        for i in range(30):
            self.svc.record_attempt(self.ip, f'user_{i}', success=False)
        self.assertFalse(self.svc.is_blocked('5.6.7.8', 'fresh_user'))

    def test_attempts_older_than_window_do_not_count(self):
        from datetime import timedelta
        from django.utils import timezone

        from apps.users.models import LoginAttempt
        old = timezone.now() - timedelta(minutes=200)
        for i in range(30):
            LoginAttempt.objects.create(
                ip=self.ip,
                username=f'user_{i}',
                attempt_time=old,
                success=False,
            )
        self.assertFalse(self.svc.is_blocked(self.ip, 'fresh_user'))