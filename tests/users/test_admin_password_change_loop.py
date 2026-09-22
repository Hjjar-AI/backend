# tests/users/test_admin_password_change_loop.py
"""
The full admin password-change loop.

The middleware tests cover the redirect. The view tests cover the
flag clearing. Neither covers the loop as a whole: an admin with
must_change_password=True who successfully changes their password
must land on the admin index and stay there. Without the wrapper
that clears the flag, the middleware would redirect every
subsequent GET back to the change form — an infinite loop with no
user-visible error and no way out short of a DB edit.
"""
from django.test import Client

from tests.base import CacheClearingTestCase
from tests.factories import make_admin


class AdminPasswordChangeLoopTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.admin = make_admin('the_admin', 'old-admin-pw-1234')
        self.admin.must_change_password = True
        self.admin.save()

    def test_complete_loop_lands_on_admin_index(self):
        self.client.force_login(self.admin)

        # Step 1: any /admin/ page redirects to the change form.
        resp = self.client.get('/admin/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/admin/password_change/', resp.url)

        # Step 2: a valid change clears the flag and redirects.
        resp = self.client.post(
            '/admin/password_change/',
            {
                'old_password': 'old-admin-pw-1234',
                'new_password1': 'New-Admin-Pass-9876',
                'new_password2': 'New-Admin-Pass-9876',
            },
        )
        self.assertEqual(resp.status_code, 302)

        self.admin.refresh_from_db()
        self.assertFalse(self.admin.must_change_password)

        # Step 3: /admin/ now returns 200 rather than redirecting.
        resp = self.client.get('/admin/')
        self.assertEqual(resp.status_code, 200)

    def test_flag_survives_a_failed_change(self):
        """
        A wrong old password must not clear the flag. Otherwise a
        user could disable their own forced change by submitting
        garbage and then navigating away.
        """
        self.client.force_login(self.admin)

        resp = self.client.post(
            '/admin/password_change/',
            {
                'old_password': 'wrong',
                'new_password1': 'New-Admin-Pass-9876',
                'new_password2': 'New-Admin-Pass-9876',
            },
        )
        # Form with errors → 200, not 302.
        self.assertEqual(resp.status_code, 200)

        self.admin.refresh_from_db()
        self.assertTrue(self.admin.must_change_password)

        # GET /admin/ still redirects.
        resp = self.client.get('/admin/')
        self.assertEqual(resp.status_code, 302)