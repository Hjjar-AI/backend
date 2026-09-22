# tests/users/test_user_model.py
from datetime import timedelta

from django.utils import timezone

from apps.users.models import User
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_admin, make_stub


class StubCreationTests(CacheClearingTestCase):
    def test_stub_is_inactive_with_unusable_password(self):
        # Usernames must pass USERNAME_REGEX — no hyphens, no dots,
        # at least 3 chars.
        stub = make_stub('external_1')
        self.assertTrue(stub.is_stub)
        self.assertFalse(stub.is_active)
        # An unusable password never matches — not even the empty string.
        self.assertFalse(stub.check_password(''))
        self.assertFalse(stub.check_password('anything'))

    def test_create_stub_is_idempotent(self):
        a = User.objects.create_stub('external_2')
        b = User.objects.create_stub('external_2')
        self.assertEqual(a.pk, b.pk)


class ExpiryTests(CacheClearingTestCase):
    def test_no_expires_at_is_never_expired(self):
        u = make_user()
        self.assertFalse(u.is_expired)

    def test_past_expires_at_is_expired(self):
        u = make_user(
            expires_at=timezone.now() - timedelta(days=1),
            auto_renew_days=0,
        )
        self.assertTrue(u.is_expired)

    def test_admin_bypasses_expiry(self):
        admin = make_admin()
        admin.expires_at = timezone.now() - timedelta(days=30)
        admin.save()
        self.assertFalse(admin.is_expired)

    def test_capability_override_bypasses_expiry(self):
        u = make_user(
            expires_at=timezone.now() - timedelta(days=1),
            capabilities={'system.bypass_expiry': True},
        )
        self.assertFalse(u.is_expired)


class RenewTests(CacheClearingTestCase):
    def test_renew_extends_expiry_when_eligible(self):
        u = make_user(
            expires_at=timezone.now() - timedelta(days=1),
            auto_renew_days=30,
        )
        self.assertTrue(u.renew_if_eligible())

    def test_renew_is_noop_when_not_yet_expired(self):
        u = make_user(
            expires_at=timezone.now() + timedelta(days=10),
            auto_renew_days=30,
        )
        self.assertFalse(u.renew_if_eligible())

    def test_renew_is_noop_without_auto_renew_days(self):
        u = make_user(
            expires_at=timezone.now() - timedelta(days=1),
            auto_renew_days=0,
        )
        self.assertFalse(u.renew_if_eligible())

    def test_renew_is_noop_for_bypass_holder(self):
        u = make_user(
            expires_at=timezone.now() - timedelta(days=1),
            auto_renew_days=30,
            capabilities={'system.bypass_expiry': True},
        )
        self.assertFalse(u.renew_if_eligible())


class AuthorRankTests(CacheClearingTestCase):
    def test_newcomer_below_threshold(self):
        u = make_user(questions_count=1, trust_score=100.0)
        self.assertEqual(u.author_rank, 'newcomer')

    def test_apprentice_threshold(self):
        u = make_user(questions_count=5, trust_score=0.0)
        self.assertEqual(u.author_rank, 'apprentice')

    def test_admin_rank_short_circuits(self):
        admin = make_admin()
        admin.questions_count = 0
        self.assertEqual(admin.author_rank, 'admin')