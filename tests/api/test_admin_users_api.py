# tests/api/test_admin_users_api.py
from rest_framework.test import APIClient

from apps.users.models import User
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_admin, make_stub, make_question


class AdminUserListAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)

    def test_requires_admin_users_capability(self):
        member = make_user('member_a')
        self.client.force_login(member)
        resp = self.client.get('/api/v1/auth/admin/users/')
        self.assertEqual(resp.status_code, 403)

    def test_list_excludes_stubs(self):
        make_user('regular_1')
        make_user('regular_2')
        make_stub('stub_author')
        resp = self.client.get('/api/v1/auth/admin/users/')
        self.assertEqual(resp.status_code, 200)
        usernames = {item['username'] for item in resp.json()['data']['items']}
        self.assertIn('regular_1', usernames)
        self.assertIn('admin_a', usernames)
        self.assertNotIn('stub_author', usernames)

    def test_pagination_meta_present(self):
        for i in range(25):
            make_user(f'u{i:03d}')
        resp = self.client.get('/api/v1/auth/admin/users/')
        meta = resp.json()['data']
        self.assertIn('total', meta)
        self.assertIn('page', meta)
        self.assertIn('per_page', meta)
        self.assertGreaterEqual(meta['total'], 25)


class AdminUserCreateAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)

    def test_create_requires_admin_password_reauth(self):
        resp = self.client.post(
            '/api/v1/auth/admin/users/',
            {
                'username': 'new_user',
                'password': 'New-User-Pass-1234',
                'role': 'member',
                'admin_password': 'wrong',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_create_success(self):
        resp = self.client.post(
            '/api/v1/auth/admin/users/',
            {
                'username': 'new_user',
                'password': 'New-User-Pass-1234',
                'full_name': 'New User',
                'role': 'member',
                'admin_password': 'admin-pw-1234',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(User.objects.filter(username='new_user').exists())

    def test_created_user_is_not_stub(self):
        self.client.post(
            '/api/v1/auth/admin/users/',
            {
                'username': 'new_user',
                'password': 'New-User-Pass-1234',
                'role': 'member',
                'admin_password': 'admin-pw-1234',
            },
            format='json',
        )
        u = User.objects.get(username='new_user')
        self.assertFalse(u.is_stub)

    def test_duplicate_username_rejected(self):
        make_user('taken')
        resp = self.client.post(
            '/api/v1/auth/admin/users/',
            {
                'username': 'taken',
                'password': 'New-User-Pass-1234',
                'role': 'member',
                'admin_password': 'admin-pw-1234',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)


class AdminUserDetailAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.other = make_admin('admin_b', 'admin-pw-1234')
        self.target = make_user('target')
        self.client.force_login(self.admin)

    def test_cannot_edit_self(self):
        resp = self.client.put(
            f'/api/v1/auth/admin/users/{self.admin.id}/',
            {'full_name': 'X', 'admin_password': 'admin-pw-1234'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_cannot_delete_self(self):
        resp = self.client.delete(
            f'/api/v1/auth/admin/users/{self.admin.id}/',
            {'admin_password': 'admin-pw-1234'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_update_requires_admin_password(self):
        resp = self.client.put(
            f'/api/v1/auth/admin/users/{self.target.id}/',
            {'full_name': 'Changed'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_update_full_name(self):
        resp = self.client.put(
            f'/api/v1/auth/admin/users/{self.target.id}/',
            {
                'full_name': 'Changed Name',
                'admin_password': 'admin-pw-1234',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        self.assertEqual(self.target.full_name, 'Changed Name')

    def test_demote_other_admin_when_two_exist(self):
        resp = self.client.put(
            f'/api/v1/auth/admin/users/{self.other.id}/',
            {'role': 'member', 'admin_password': 'admin-pw-1234'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.other.refresh_from_db()
        self.assertEqual(self.other.role, 'member')

    def test_last_admin_guard_fires(self):
        """
        A caller who holds `admin.users` but is NOT themselves an
        active admin, demoting the only active admin, must be
        refused.

        The setup below promotes `target` to admin, then removes
        every OTHER active admin so `target` is the last one. The
        actor is a plain member who holds `admin.users` via a
        per-user capability override — this is the only way to
        reach the view's guard with a non-admin caller under the
        default role set.
        """
        # Make target an admin, deactivate or remove every other admin.
        self.target.role = 'admin'
        self.target.save()
        self.admin.is_active = False
        self.admin.save()
        self.other.delete()

        actor = make_user(
            'override_actor',
            'actor-pw-1234',
            capabilities={'admin.users': True},
        )
        self.client.force_login(actor)

        resp = self.client.put(
            f'/api/v1/auth/admin/users/{self.target.id}/',
            {'role': 'member', 'admin_password': 'actor-pw-1234'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.target.refresh_from_db()
        self.assertEqual(self.target.role, 'admin')

    def test_delete_user_with_authored_questions_returns_409(self):
        make_question(owner=self.target)
        resp = self.client.delete(
            f'/api/v1/auth/admin/users/{self.target.id}/',
            {'admin_password': 'admin-pw-1234'},
            format='json',
        )
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(User.objects.filter(id=self.target.id).exists())

    def test_delete_clean_user_succeeds(self):
        resp = self.client.delete(
            f'/api/v1/auth/admin/users/{self.target.id}/',
            {'admin_password': 'admin-pw-1234'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(id=self.target.id).exists())


class AdminToggleUserAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.target = make_user('target')
        self.client.force_login(self.admin)

    def test_cannot_toggle_self(self):
        resp = self.client.post(
            f'/api/v1/auth/admin/users/{self.admin.id}/toggle/',
            {'admin_password': 'admin-pw-1234'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_toggle_stub_is_refused(self):
        stub = make_stub('stub_author')
        resp = self.client.post(
            f'/api/v1/auth/admin/users/{stub.id}/toggle/',
            {'admin_password': 'admin-pw-1234'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        stub.refresh_from_db()
        self.assertFalse(stub.is_active)

    def test_toggle_deactivates_then_reactivates(self):
        self.client.post(
            f'/api/v1/auth/admin/users/{self.target.id}/toggle/',
            {'admin_password': 'admin-pw-1234'},
            format='json',
        )
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active)

        self.client.post(
            f'/api/v1/auth/admin/users/{self.target.id}/toggle/',
            {'admin_password': 'admin-pw-1234'},
            format='json',
        )
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)

    def test_wrong_admin_password_refused(self):
        resp = self.client.post(
            f'/api/v1/auth/admin/users/{self.target.id}/toggle/',
            {'admin_password': 'wrong'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)


class AdminResetPasswordAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.target = make_user('target', 'old-pw-1234')
        self.client.force_login(self.admin)

    def test_generated_temp_password_mode(self):
        resp = self.client.post(
            f'/api/v1/auth/admin/users/{self.target.id}/reset-password/',
            {'admin_password': 'admin-pw-1234'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        temp = resp.json()['data']['temp_password']
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password(temp))
        self.assertTrue(self.target.must_change_password)

    def test_admin_supplied_password_mode(self):
        resp = self.client.post(
            f'/api/v1/auth/admin/users/{self.target.id}/reset-password/',
            {
                'admin_password': 'admin-pw-1234',
                'new_password': 'Admin-Supplied-Pass-99',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password('Admin-Supplied-Pass-99'))
        self.assertTrue(self.target.must_change_password)

    def test_weak_supplied_password_rejected(self):
        resp = self.client.post(
            f'/api/v1/auth/admin/users/{self.target.id}/reset-password/',
            {'admin_password': 'admin-pw-1234', 'new_password': '123'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)


class AdminActiveUsersAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)

    def test_returns_empty_when_nobody_active(self):
        resp = self.client.get('/api/v1/auth/admin/active-users/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['users'], {})

    def test_requires_admin_active_users_capability(self):
        member = make_user('member_a')
        self.client.force_login(member)
        resp = self.client.get('/api/v1/auth/admin/active-users/')
        self.assertEqual(resp.status_code, 403)