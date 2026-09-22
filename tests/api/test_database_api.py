# tests/api/test_database_api.py
"""
Every view in apps/database/views.py except the two that shell out
to mysqldump/mysql (create_backup, restore_backup).

The backup/restore *services* are stubbed via mock so the tests
exercise the view's gate logic and error passthrough without
touching the DBMS shell.

ERROR PASSTHROUGH
-----------------
Every view in this module now checks `if 'error' in result` and
routes the service error through `api_error`. An earlier revision
had `ListBackupsView` skipping that check, which meant a backend
the service does not support returned `{'error': ..., 'code': 500}`
wrapped in a 200 response. `test_service_error_returns_500` below
is the guard against re-introducing that.
"""
import json
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_admin


def _envelope(questions=()):
    return {
        'meta': {
            'format': 'mukhtabir-questions',
            'version': 2,
            'exported_at': '2026-01-01T00:00:00',
            'includes_images': False,
            'user_map': {},
            'counts': {},
        },
        'categories': [],
        'tags': [],
        'cases': [],
        'questions': list(questions),
    }


def _upload(payload):
    raw = json.dumps(payload).encode('utf-8')
    return SimpleUploadedFile(
        'state.json', raw, content_type='application/json',
    )


class DatabaseAdminGateTests(CacheClearingTestCase):
    """
    Every database endpoint gates on `admin.database`. A moderator
    does not hold it — only admins do.
    """
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.member = make_user('member_a')
        self.mod = make_user('mod_user', role='moderator')
        self.admin = make_admin('admin_a', 'admin-pw-1234')

    def _paths(self):
        return [
            ('get', '/api/v1/database/info/'),
            ('get', '/api/v1/database/backups/'),
            ('get', '/api/v1/database/export/csv/'),
            ('get', '/api/v1/database/export/csv/verified/'),
            ('get', '/api/v1/database/export/state/'),
        ]

    def test_member_refused_on_all(self):
        self.client.force_login(self.member)
        for method, url in self._paths():
            with self.subTest(url=url):
                resp = getattr(self.client, method)(url)
                self.assertEqual(resp.status_code, 403)

    def test_moderator_refused_on_all(self):
        self.client.force_login(self.mod)
        for method, url in self._paths():
            with self.subTest(url=url):
                resp = getattr(self.client, method)(url)
                self.assertEqual(resp.status_code, 403)

    def test_anonymous_refused_on_all(self):
        for method, url in self._paths():
            with self.subTest(url=url):
                resp = getattr(self.client, method)(url)
                self.assertIn(resp.status_code, (401, 403))


class DatabaseInfoViewTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)

    def test_passes_through_service_success(self):
        fake_info = {
            'file_path': '/var/lib/mysql/quiz',
            'file_size': '1024 KB',
            'database_type': 'MariaDB',
            'total_questions': 5,
        }
        with patch(
            'apps.database.views.BackupService.get_info',
            return_value=fake_info,
        ):
            resp = self.client.get('/api/v1/database/info/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data'], fake_info)

    def test_passes_through_service_error(self):
        with patch(
            'apps.database.views.BackupService.get_info',
            return_value={'error': 'DB offline', 'code': 500},
        ):
            resp = self.client.get('/api/v1/database/info/')
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()['message'], 'DB offline')


class ListBackupsViewTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)

    def test_returns_service_payload(self):
        fake = {'items': [
            {'name': 'backup_1.sql', 'size': '100 KB', 'modified': '2026-01-01'}
        ], 'total': 1}
        with patch(
            'apps.database.views.BackupService.list_backups',
            return_value=fake,
        ):
            resp = self.client.get('/api/v1/database/backups/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data'], fake)

    def test_service_error_returns_500(self):
        """
        Regression guard for the earlier revision where
        ListBackupsView skipped the `if 'error' in result` check
        and wrapped the error dict in a 200 response. A client
        checking `response.ok` would then treat a hard backend
        failure as a successful empty list.
        """
        with patch(
            'apps.database.views.BackupService.list_backups',
            return_value={'error': 'not supported', 'code': 500},
        ):
            resp = self.client.get('/api/v1/database/backups/')
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()['message'], 'not supported')
        # The error must NOT be hidden inside a 200 `data` envelope.
        self.assertNotIn('data', resp.json())


class RestoreBackupViewTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)

    def test_missing_body_returns_400(self):
        resp = self.client.post('/api/v1/database/restore/', {}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_wrong_admin_password_returns_403(self):
        resp = self.client.post(
            '/api/v1/database/restore/',
            {'backup_name': 'x.sql', 'admin_password': 'wrong'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_valid_credentials_reach_service(self):
        with patch(
            'apps.database.views.BackupService.restore_backup',
            return_value={'message': 'ok'},
        ) as mock_restore:
            resp = self.client.post(
                '/api/v1/database/restore/',
                {
                    'backup_name': 'x.sql',
                    'admin_password': 'admin-pw-1234',
                },
                format='json',
            )
        self.assertEqual(resp.status_code, 200)
        mock_restore.assert_called_once_with('x.sql')


class ClearDatabaseViewTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)

    def test_missing_password_returns_400(self):
        resp = self.client.post('/api/v1/database/clear/', {}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_wrong_password_returns_403(self):
        resp = self.client.post(
            '/api/v1/database/clear/',
            {'admin_password': 'wrong'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_correct_password_reaches_service(self):
        with patch(
            'apps.database.views.BackupService.clear_database',
            return_value={'message': 'cleared'},
        ) as mock_clear:
            resp = self.client.post(
                '/api/v1/database/clear/',
                {'admin_password': 'admin-pw-1234'},
                format='json',
            )
        self.assertEqual(resp.status_code, 200)
        mock_clear.assert_called_once()


class ExportDatabaseViewTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        import shutil
        import tempfile
        from django.test import override_settings
        self.tmpdir = tempfile.mkdtemp()
        self.override = override_settings(EXPORT_FOLDER=self.tmpdir)
        self.override.enable()

        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)
        from tests.factories import make_question
        make_question(owner=self.admin)

    def tearDown(self):
        self.override.disable()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        super().tearDown()

    def test_csv_export_returns_file(self):
        resp = self.client.get('/api/v1/database/export/csv/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('attachment', resp['Content-Disposition'])
        self.assertIn('.csv', resp['Content-Disposition'])

    def test_verified_only_export(self):
        resp = self.client.get('/api/v1/database/export/csv/verified/')
        # No verified questions exist, so the service returns 404.
        self.assertEqual(resp.status_code, 404)

    def test_unknown_format_returns_400(self):
        resp = self.client.get('/api/v1/database/export/nonsense/')
        self.assertEqual(resp.status_code, 400)

    @patch('apps.database.views.ExportService.export_questions')
    def test_pdf_theme_query_param_is_forwarded(self, export_questions):
        filepath = Path(self.tmpdir) / 'themed.pdf'
        filepath.write_bytes(b'%PDF-test')
        export_questions.return_value = {
            'filepath': str(filepath),
            'filename': 'themed.pdf',
        }

        resp = self.client.get('/api/v1/database/export/pdf/?theme=dark')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(export_questions.call_args.kwargs['theme'], 'dark')
        b''.join(resp.streaming_content)

    def test_state_export_returns_json(self):
        resp = self.client.get('/api/v1/database/export/state/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('application/json', resp['Content-Type'])

    def test_state_export_query_params_passed_through(self):
        resp = self.client.get(
            '/api/v1/database/export/state/?include_images=false&verified_only=false'
        )
        self.assertEqual(resp.status_code, 200)


class ImportStateViewTests(CacheClearingTestCase):
    """
    The view wraps ImportService.import_state and owns:
      • the admin password re-auth gate for replace+write
      • the analyze / dry_run / write mode dispatch
      • the mapping JSON parse and shape check
      • the 200-vs-201 status code decision

    verify_upload_mime is patched so the tests do not depend on
    python-magic being installed.
    """
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)
        self._mime_patcher = patch(
            'apps.questions.services.importing.state_import.entrypoint.verify_upload_mime',
            return_value=None,
        )
        self._mime_patcher.start()

    def tearDown(self):
        self._mime_patcher.stop()
        super().tearDown()

    def _post(self, data):
        return self.client.post(
            '/api/v1/database/import/state/', data, format='multipart',
        )

    def test_no_file_returns_400(self):
        resp = self._post({})
        self.assertEqual(resp.status_code, 400)

    def test_analyze_returns_200(self):
        resp = self._post({
            'file': _upload(_envelope()),
            'analyze': 'true',
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertTrue(data['dry_run'])
        self.assertIn('unknown_authors', data)

    def test_dry_run_returns_200(self):
        resp = self._post({
            'file': _upload(_envelope()),
            'dry_run': 'true',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['data']['dry_run'])

    def test_merge_write_returns_201(self):
        resp = self._post({'file': _upload(_envelope()), 'mode': 'merge'})
        self.assertEqual(resp.status_code, 201)

    def test_replace_write_requires_admin_password(self):
        resp = self._post({
            'file': _upload(_envelope()), 'mode': 'replace',
        })
        self.assertEqual(resp.status_code, 403)

    def test_replace_write_wrong_password_returns_403(self):
        resp = self._post({
            'file': _upload(_envelope()), 'mode': 'replace',
            'admin_password': 'wrong',
        })
        self.assertEqual(resp.status_code, 403)

    def test_replace_write_correct_password_returns_201(self):
        resp = self._post({
            'file': _upload(_envelope()), 'mode': 'replace',
            'admin_password': 'admin-pw-1234',
        })
        self.assertEqual(resp.status_code, 201)

    def test_replace_dry_run_does_not_require_password(self):
        resp = self._post({
            'file': _upload(_envelope()), 'mode': 'replace',
            'dry_run': 'true',
        })
        self.assertEqual(resp.status_code, 200)

    def test_invalid_mapping_json_returns_400(self):
        resp = self._post({
            'file': _upload(_envelope()),
            'mapping': 'not-json',
        })
        self.assertEqual(resp.status_code, 400)

    def test_mapping_not_object_returns_400(self):
        resp = self._post({
            'file': _upload(_envelope()),
            'mapping': json.dumps(['list', 'not', 'dict']),
        })
        self.assertEqual(resp.status_code, 400)

    def test_valid_mapping_passes_through(self):
        resp = self._post({
            'file': _upload(_envelope()),
            'mapping': json.dumps({'External': {'action': 'null'}}),
        })
        self.assertEqual(resp.status_code, 201)
