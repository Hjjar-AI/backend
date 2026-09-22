# tests/core/test_health_and_config_api.py
"""
HealthView, PublicConfigView, TipsView, AdminSettingsView,
RefreshAuthorRanksView, SeedSampleQuestionsView.
"""
from unittest.mock import patch

from rest_framework.test import APIClient

from apps.core.models import Setting, Tip
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_admin


class HealthViewTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_healthy_returns_200_and_ok(self):
        resp = self.client.get('/api/v1/health/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['checks']['database'], 'ok')
        self.assertEqual(data['checks']['cache'], 'ok')

    def test_database_probe_failure_returns_503(self):
        from django.db import DatabaseError
        with patch(
            'apps.core.views.connection.cursor',
            side_effect=DatabaseError('simulated'),
        ):
            resp = self.client.get('/api/v1/health/')
        self.assertEqual(resp.status_code, 503)
        data = resp.json()['data']
        self.assertEqual(data['status'], 'degraded')
        self.assertIn('error', data['checks']['database'])

    def test_cache_probe_failure_returns_503(self):
        """
        Patch the view's LOCAL reference to the cache object, not the
        shared django.core.cache.cache instance. DRF's throttling
        uses the same cache and calls cache.set during
        check_throttles — patching cache.set directly would fire the
        mock before the view code even runs, producing an uncaught
        RuntimeError rather than the 503 the test expects.
        """
        with patch('apps.core.views.cache') as mock_cache:
            mock_cache.set.side_effect = RuntimeError('cache down')
            resp = self.client.get('/api/v1/health/')
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()['data']['checks']['cache'][:6], 'error:')

    def test_unauthenticated_allowed(self):
        resp = self.client.get('/api/v1/health/')
        self.assertEqual(resp.status_code, 200)


class PublicConfigViewTests(CacheClearingTestCase):
    def test_returns_config_unauthenticated(self):
        resp = APIClient().get('/api/v1/config/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        for key in ('max_quiz_questions', 'max_choices', 'items_per_page', 'roles'):
            self.assertIn(key, data)


class TipsViewTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = make_user('alice')
        self.client.force_login(self.user)

    def test_requires_auth(self):
        self.client.logout()
        resp = self.client.get('/api/v1/tips/')
        self.assertIn(resp.status_code, (401, 403))

    def test_default_locale_is_ar(self):
        Tip.objects.create(text='نصيحة عربية', locale='ar', is_active=True)
        resp = self.client.get('/api/v1/tips/')
        data = resp.json()['data']
        self.assertEqual(data['locale'], 'ar')
        self.assertIn('نصيحة عربية', data['tips'])

    def test_english_locale_requested(self):
        Tip.objects.create(text='English tip', locale='en', is_active=True)
        resp = self.client.get('/api/v1/tips/?locale=en')
        data = resp.json()['data']
        self.assertEqual(data['locale'], 'en')
        self.assertIn('English tip', data['tips'])

    def test_unknown_locale_falls_back_to_default(self):
        Tip.objects.create(text='افتراضي', locale='ar', is_active=True)
        resp = self.client.get('/api/v1/tips/?locale=fr')
        data = resp.json()['data']
        self.assertEqual(data['locale'], 'ar')
        self.assertIn('افتراضي', data['tips'])

    def test_empty_locale_falls_back_to_default(self):
        Tip.objects.create(text='fallback', locale='ar', is_active=True)
        resp = self.client.get('/api/v1/tips/?locale=en')
        data = resp.json()['data']
        self.assertEqual(data['locale'], 'ar')
        self.assertIn('fallback', data['tips'])

    def test_inactive_tips_excluded(self):
        Tip.objects.create(text='active tip', locale='ar', is_active=True)
        Tip.objects.create(text='inactive tip', locale='ar', is_active=False)
        resp = self.client.get('/api/v1/tips/')
        tips = resp.json()['data']['tips']
        self.assertIn('active tip', tips)
        self.assertNotIn('inactive tip', tips)


class AdminSettingsViewTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.member = make_user('member_a')
        self.client.force_login(self.admin)

    def test_member_refused(self):
        self.client.force_login(self.member)
        resp = self.client.get('/api/v1/admin/settings/')
        self.assertEqual(resp.status_code, 403)

    def test_get_returns_known_keys(self):
        Setting.objects.create(key='default_expiry_days', value='30')
        resp = self.client.get('/api/v1/admin/settings/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(data['default_expiry_days'], '30')
        self.assertEqual(data['default_renewal_days'], '30')
        self.assertEqual(data['exam_duration_minutes'], '60')

    def test_post_updates_numeric_settings(self):
        resp = self.client.post(
            '/api/v1/admin/settings/',
            {
                'default_expiry_days': 45,
                'exam_duration_minutes': 90,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            Setting.objects.get(key='default_expiry_days').value, '45',
        )
        self.assertEqual(
            Setting.objects.get(key='exam_duration_minutes').value, '90',
        )

    def test_post_rejects_negative_values(self):
        resp = self.client.post(
            '/api/v1/admin/settings/',
            {'default_expiry_days': -1},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_post_rejects_non_numeric(self):
        resp = self.client.post(
            '/api/v1/admin/settings/',
            {'default_expiry_days': 'abc'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_post_ignores_unknown_keys(self):
        resp = self.client.post(
            '/api/v1/admin/settings/',
            {'unknown_key': 'value'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Setting.objects.filter(key='unknown_key').exists())


class RefreshAuthorRanksViewTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.member = make_user('member_a')
        self.client.force_login(self.admin)

    def test_member_refused(self):
        self.client.force_login(self.member)
        resp = self.client.post('/api/v1/admin/refresh-author-ranks/')
        self.assertEqual(resp.status_code, 403)

    def test_admin_gets_counts(self):
        from tests.factories import make_question
        make_question(owner=make_user('author'))
        resp = self.client.post('/api/v1/admin/refresh-author-ranks/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertIn('scanned', data)
        self.assertIn('updated', data)


class SeedSampleQuestionsViewTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.member = make_user('member_a')

    def test_member_refused(self):
        self.client.force_login(self.member)
        resp = self.client.post('/api/v1/admin/seed-sample-questions/')
        self.assertEqual(resp.status_code, 403)

    def test_admin_reaches_command(self):
        from unittest.mock import patch as _patch
        self.client.force_login(self.admin)
        with _patch(
            'apps.core.views.call_command',
            return_value=None,
        ) as mock_call:
            resp = self.client.post('/api/v1/admin/seed-sample-questions/')
        self.assertEqual(resp.status_code, 200)
        mock_call.assert_called_once()
        kwargs = mock_call.call_args.kwargs
        self.assertEqual(kwargs.get('only'), ['questions'])
