# tests/users/test_admin_password_change_view.py
"""
The wrapper around Django admin's password-change view that clears
`must_change_password` on successful change.

Without this wrapper, MustChangePasswordMiddleware's admin-side
redirect would loop: Django admin saves the new password, but the
custom flag stays True, so the next request to /admin/ redirects
straight back to the change-password form.

LOGIN-URL ASSERTIONS
--------------------
Django's admin `login` view redirects via `redirect_to_login` using
`settings.LOGIN_URL` when its own `login_url` is unset. On a default
Django install that is `/accounts/login/`; on this project's
settings it could be either. The test below asserts only that:
  • the response is a 302,
  • the redirect target contains `login`,
  • the `next=` parameter points back at the password-change URL.

That is the contract that matters — the login page is reached, and
after logging in the user lands back on the change-password form.
The exact host path of the login URL is a settings concern, not a
behavior one.
"""
from django.test import Client

from tests.base import CacheClearingTestCase
from tests.factories import make_admin


class AdminPasswordChangeViewTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.admin = make_admin('the_admin', 'old-admin-pw-1234')
        self.admin.must_change_password = True
        self.admin.save()

    def test_anonymous_request_redirects_to_login(self):
        resp = self.client.get('/admin/password_change/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp.url)
        # The next= parameter round-trips back to the password-change
        # URL so the user lands where they intended after logging in.
        self.assertIn('/admin/password_change/', resp.url)

    def test_authenticated_get_renders_form(self):
        self.client.force_login(self.admin)
        resp = self.client.get('/admin/password_change/')
        self.assertEqual(resp.status_code, 200)

    def test_successful_change_clears_flag(self):
        self.client.force_login(self.admin)
        resp = self.client.post(
            '/admin/password_change/',
            {
                'old_password': 'old-admin-pw-1234',
                'new_password1': 'New-Admin-Pass-9876',
                'new_password2': 'New-Admin-Pass-9876',
            },
        )
        # Successful change redirects to the done page.
        self.assertEqual(resp.status_code, 302)
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.must_change_password)

    def test_wrong_old_password_does_not_clear_flag(self):
        self.client.force_login(self.admin)
        self.client.post(
            '/admin/password_change/',
            {
                'old_password': 'wrong',
                'new_password1': 'New-Admin-Pass-9876',
                'new_password2': 'New-Admin-Pass-9876',
            },
        )
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.must_change_password)

    def test_mismatched_new_passwords_do_not_clear_flag(self):
        self.client.force_login(self.admin)
        self.client.post(
            '/admin/password_change/',
            {
                'old_password': 'old-admin-pw-1234',
                'new_password1': 'New-Admin-Pass-9876',
                'new_password2': 'Different-Pass-0000',
            },
        )
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.must_change_password)