# tests/api/test_permissions_api.py
from rest_framework.test import APIClient

from apps.users.capabilities import CAPABILITIES, DEFAULT_ROLE_CAPABILITIES
from apps.users.models import RoleCapabilities
from apps.users.services.permission_service import (
    role_capabilities, invalidate_role_capabilities,
)
from tests.base import CacheClearingTestCase
from tests.factories import make_admin, make_user, make_stub


class RoleCapabilitiesAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)

    def test_requires_admin_permissions_capability(self):
        member = make_user('member_a')
        self.client.force_login(member)
        resp = self.client.get('/api/v1/auth/admin/permissions/')
        self.assertEqual(resp.status_code, 403)

    def test_get_returns_capabilities_groups_roles(self):
        resp = self.client.get('/api/v1/auth/admin/permissions/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertIn('capabilities', data)
        self.assertIn('groups', data)
        self.assertIn('roles', data)

        # Capabilities list must match the registry exactly.
        self.assertEqual(
            set(data['capabilities']), set(CAPABILITIES),
        )

        # Every group label is present.
        group_labels = {g['label'] for g in data['groups']}
        for expected in (
            'Questions', 'Verification & taxonomy', 'Tests & study',
            'Master exams', 'Social & planning', 'Administration',
            'Analytics', 'System',
        ):
            self.assertIn(expected, group_labels)

        # Roles dict falls back to defaults when no DB row exists.
        self.assertEqual(
            set(data['roles']['member']),
            set(DEFAULT_ROLE_CAPABILITIES['member']),
        )

    def test_put_updates_member_role(self):
        new_caps = sorted(DEFAULT_ROLE_CAPABILITIES['member'] | {'questions.verify'})
        resp = self.client.put(
            '/api/v1/auth/admin/permissions/',
            {'role': 'member', 'capabilities': new_caps},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)

        row = RoleCapabilities.objects.get(role='member')
        self.assertIn('questions.verify', row.capabilities)

    def test_put_refuses_admin_role(self):
        resp = self.client.put(
            '/api/v1/auth/admin/permissions/',
            {'role': 'admin', 'capabilities': []},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            RoleCapabilities.objects.filter(
                role='admin', capabilities=[],
            ).exists()
        )

    def test_put_rejects_unknown_capability(self):
        resp = self.client.put(
            '/api/v1/auth/admin/permissions/',
            {'role': 'member', 'capabilities': ['ghost.cap']},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_put_deduplicates(self):
        resp = self.client.put(
            '/api/v1/auth/admin/permissions/',
            {
                'role': 'member',
                'capabilities': ['questions.create', 'questions.create',
                                 'questions.create'],
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        row = RoleCapabilities.objects.get(role='member')
        self.assertEqual(row.capabilities.count('questions.create'), 1)

    def test_put_refuses_unknown_role(self):
        resp = self.client.put(
            '/api/v1/auth/admin/permissions/',
            {'role': 'no_such_role', 'capabilities': []},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)


class UserCapabilitiesAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.target = make_user('target')
        self.client.force_login(self.admin)

    def test_get_returns_role_and_resolved_sets(self):
        resp = self.client.get(
            f'/api/v1/auth/admin/permissions/users/{self.target.id}/'
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(data['role'], 'member')
        self.assertIn('role_capabilities', data)
        self.assertIn('overrides', data)
        self.assertIn('resolved', data)

    def test_put_sets_and_clears_overrides(self):
        resp = self.client.put(
            f'/api/v1/auth/admin/permissions/users/{self.target.id}/',
            {'capabilities': {'questions.verify': True}},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        self.assertEqual(
            self.target.capabilities, {'questions.verify': True},
        )
        self.assertIn('questions.verify', resp.json()['data']['resolved'])

    def test_put_null_value_clears_that_override(self):
        self.target.capabilities = {'questions.verify': True}
        self.target.save()
        resp = self.client.put(
            f'/api/v1/auth/admin/permissions/users/{self.target.id}/',
            {'capabilities': {'questions.verify': None}},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        self.assertEqual(self.target.capabilities, {})

    def test_put_rejects_unknown_capability(self):
        resp = self.client.put(
            f'/api/v1/auth/admin/permissions/users/{self.target.id}/',
            {'capabilities': {'ghost.cap': True}},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_put_rejects_non_dict(self):
        resp = self.client.put(
            f'/api/v1/auth/admin/permissions/users/{self.target.id}/',
            {'capabilities': 'not-a-dict'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_stub_is_404_on_get_and_put(self):
        stub = make_stub('stub_author')
        resp = self.client.get(
            f'/api/v1/auth/admin/permissions/users/{stub.id}/'
        )
        self.assertEqual(resp.status_code, 404)

        resp = self.client.put(
            f'/api/v1/auth/admin/permissions/users/{stub.id}/',
            {'capabilities': {}},
            format='json',
        )
        self.assertEqual(resp.status_code, 404)

    def test_capability_override_takes_effect_immediately(self):
        """
        The signal hook and the explicit delattr in the view must
        both fire so the resolved set reflects the write without a
        process restart.
        """
        self.assertNotIn('questions.verify', self.target.resolved_capabilities())
        self.client.put(
            f'/api/v1/auth/admin/permissions/users/{self.target.id}/',
            {'capabilities': {'questions.verify': True}},
            format='json',
        )
        self.target.refresh_from_db()
        # Clear the memo by reloading from a fresh fetch.
        from apps.users.models import User
        fresh = User.objects.get(id=self.target.id)
        self.assertIn('questions.verify', fresh.resolved_capabilities())