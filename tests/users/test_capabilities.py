# tests/users/test_capabilities.py
from apps.users.capabilities import (
    CAPABILITIES, CAPABILITY_GROUPS, DEFAULT_ROLE_CAPABILITIES,
)
from apps.users.models import RoleCapabilities
from apps.users.services.permission_service import (
    resolve_for_user, invalidate_role_capabilities, role_capabilities,
)
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_admin


class CapabilityRegistryTests(CacheClearingTestCase):
    """
    The registry has load-time assertions; these tests re-verify
    them at test time so a future refactor that removes the asserts
    is still caught by CI.
    """

    def test_groups_match_capabilities_exactly(self):
        grouped = frozenset(c for _, g in CAPABILITY_GROUPS for c in g)
        self.assertEqual(grouped, CAPABILITIES)

    def test_every_role_has_defaults(self):
        for role in ('admin', 'moderator', 'member'):
            self.assertIn(role, DEFAULT_ROLE_CAPABILITIES)

    def test_admin_defaults_are_every_capability(self):
        self.assertEqual(DEFAULT_ROLE_CAPABILITIES['admin'], CAPABILITIES)

    def test_member_is_subset_of_moderator(self):
        self.assertTrue(
            DEFAULT_ROLE_CAPABILITIES['member'].issubset(
                DEFAULT_ROLE_CAPABILITIES['moderator']
            )
        )

    def test_removed_capabilities_are_gone(self):
        """
        Regression guard: these strings were removed in a prior
        cleanup. They must not come back without a corresponding
        view change that enforces them.
        """
        for dead in ('analytics.view_own', 'groups.use',
                     'leaderboard.view', 'planner.use',
                     'tests.use_srs', 'tests.study_now'):
            self.assertNotIn(dead, CAPABILITIES)


class RoleCapabilitiesTests(CacheClearingTestCase):
    def test_fallback_to_defaults_when_no_db_row(self):
        RoleCapabilities.objects.filter(role='member').delete()
        invalidate_role_capabilities('member')
        caps = role_capabilities('member')
        self.assertEqual(caps, set(DEFAULT_ROLE_CAPABILITIES['member']))

    def test_db_row_wins_over_default(self):
        RoleCapabilities.objects.update_or_create(
            role='member',
            defaults={'capabilities': ['questions.verify']},
        )
        invalidate_role_capabilities('member')
        caps = role_capabilities('member')
        self.assertEqual(caps, {'questions.verify'})

    def test_unknown_capability_in_db_row_is_filtered(self):
        RoleCapabilities.objects.update_or_create(
            role='member',
            defaults={'capabilities': ['questions.verify', 'ghost.cap']},
        )
        invalidate_role_capabilities('member')
        caps = role_capabilities('member')
        self.assertNotIn('ghost.cap', caps)
        self.assertIn('questions.verify', caps)

    def test_signal_invalidates_cache_on_save(self):
        # Prime the cache.
        role_capabilities('member')
        RoleCapabilities.objects.update_or_create(
            role='member', defaults={'capabilities': ['questions.verify']},
        )
        # No explicit invalidate call — the post_save signal must have
        # dropped the cache entry.
        self.assertEqual(role_capabilities('member'), {'questions.verify'})


class ResolveForUserTests(CacheClearingTestCase):
    def test_anonymous_gets_empty_set(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertEqual(resolve_for_user(AnonymousUser()), set())
        self.assertEqual(resolve_for_user(None), set())

    def test_admin_gets_full_set(self):
        self.assertEqual(
            resolve_for_user(make_admin()), set(CAPABILITIES),
        )

    def test_member_gets_defaults(self):
        self.assertEqual(
            resolve_for_user(make_user()),
            set(DEFAULT_ROLE_CAPABILITIES['member']),
        )

    def test_per_user_override_grants(self):
        u = make_user(capabilities={'questions.verify': True})
        self.assertIn('questions.verify', resolve_for_user(u))

    def test_per_user_override_revokes(self):
        u = make_user(capabilities={'questions.create': False})
        self.assertNotIn('questions.create', resolve_for_user(u))

    def test_unknown_override_key_is_ignored(self):
        u = make_user(capabilities={'ghost.cap': True})
        caps = resolve_for_user(u)
        self.assertNotIn('ghost.cap', caps)
        # Unrelated keys are unaffected.
        self.assertIn('questions.create', caps)