# tests/core/test_permissions.py
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory

from apps.core.permissions import HasCapability
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_admin


class _ViewNoCap:
    pass


class _ViewWithCap:
    required_capability = 'questions.create'


class _ViewModeratorOnlyCap:
    """
    'questions.verify' is held by moderators by default, not members.
    Used to verify the admin short-circuit passes gates that regular
    roles fail.
    """
    required_capability = 'questions.verify'


class HasCapabilityTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.perm = HasCapability()

    def _req(self, user):
        req = self.factory.get('/')
        req.user = user
        return req

    def test_unauthenticated_user_is_refused(self):
        from django.contrib.auth.models import AnonymousUser
        req = self._req(AnonymousUser())
        self.assertFalse(self.perm.has_permission(req, _ViewWithCap()))

    def test_missing_required_capability_raises_improperly_configured(self):
        """
        Fail-closed configuration: a view using HasCapability without
        declaring required_capability must raise at request time.
        """
        user = make_user()
        with self.assertRaises(ImproperlyConfigured):
            self.perm.has_permission(self._req(user), _ViewNoCap())

    def test_member_holding_capability_passes(self):
        # The 'member' role holds 'questions.create' by default.
        user = make_user()
        self.assertTrue(
            self.perm.has_permission(self._req(user), _ViewWithCap())
        )

    def test_member_lacking_capability_is_refused(self):
        user = make_user()  # member does not hold questions.verify
        self.assertFalse(
            self.perm.has_permission(
                self._req(user), _ViewModeratorOnlyCap(),
            )
        )

    def test_admin_passes_every_gate(self):
        admin = make_admin()
        self.assertTrue(
            self.perm.has_permission(self._req(admin), _ViewWithCap())
        )

    def test_admin_passes_gate_member_cannot(self):
        """
        The admin short-circuit in resolve_for_user() returns
        set(CAPABILITIES) — the full registry. This test verifies the
        pass-through for a capability a plain member lacks.

        Note: the short-circuit is bounded by the registry. A string
        NOT in CAPABILITIES (e.g. 'nonexistent.cap') is refused for
        every role, including admin, because it is not a capability
        the system recognizes. That is the correct behavior — the
        registry assertion in apps/users/capabilities.py is what
        catches typos, not the permission class.
        """
        admin = make_admin()
        self.assertTrue(
            self.perm.has_permission(
                self._req(admin), _ViewModeratorOnlyCap(),
            )
        )

    def test_unknown_capability_is_refused_even_for_admin(self):
        """
        A capability string that does not exist in the registry is
        not a grantable action. Returning True here would let a view
        with a typo'd capability name ship silently.
        """
        class _ViewTypo:
            required_capability = 'nonexistent.cap'
        admin = make_admin()
        self.assertFalse(
            self.perm.has_permission(self._req(admin), _ViewTypo())
        )