# tests/management/test_cleanup_commands.py
"""
Cleanup-flavoured management commands. `seed` and `seed_capabilities`
have their own file; this one covers the periodic housekeeping
commands and the two rank/user maintenance commands.
"""
import os
import tempfile
import time
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.core.models import PrivilegedAction
from apps.users.models import User
from tests.base import CacheClearingTestCase
from tests.factories import make_user


class CleanupPrivilegedActionsTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.old = PrivilegedAction.objects.create(
            action='test.old',
            timestamp=timezone.now() - timedelta(days=400),
        )
        self.recent = PrivilegedAction.objects.create(
            action='test.recent',
            timestamp=timezone.now() - timedelta(days=1),
        )

    def test_default_retention_removes_old_rows(self):
        call_command('cleanup_privileged_actions', stdout=StringIO())
        self.assertFalse(
            PrivilegedAction.objects.filter(id=self.old.id).exists(),
        )
        self.assertTrue(
            PrivilegedAction.objects.filter(id=self.recent.id).exists(),
        )

    def test_days_flag_overrides_retention(self):
        # Default retention is 365 days, which would delete the
        # 400-day-old row. Passing `--days 500` moves the cutoff back
        # far enough that both rows survive — proving the flag
        # overrode the setting rather than the other way around.
        call_command(
            'cleanup_privileged_actions', '--days', 500,
            stdout=StringIO(),
        )
        self.assertTrue(
            PrivilegedAction.objects.filter(id=self.old.id).exists(),
        )
        self.assertTrue(
            PrivilegedAction.objects.filter(id=self.recent.id).exists(),
        )

    def test_dry_run_does_not_delete(self):
        out = StringIO()
        call_command(
            'cleanup_privileged_actions', '--dry-run', stdout=out,
        )
        self.assertIn('would delete', out.getvalue())
        self.assertTrue(
            PrivilegedAction.objects.filter(id=self.old.id).exists(),
        )

    def test_quiet_suppresses_success_line(self):
        out = StringIO()
        call_command('cleanup_privileged_actions', '--quiet', stdout=out)
        self.assertEqual(out.getvalue().strip(), '')

    def test_dry_run_with_days(self):
        out = StringIO()
        call_command(
            'cleanup_privileged_actions', '--dry-run', '--days', 500,
            stdout=out,
        )
        self.assertIn('would delete 0', out.getvalue())


class CleanupTempFilesTests(CacheClearingTestCase):
    """
    Writes real files into a temp UPLOAD_FOLDER and verifies the
    age-based deletion. Uses override_settings so the command does
    not touch the real uploads folder.
    """
    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp()
        self.override = override_settings(
            UPLOAD_FOLDER=self.tmpdir,
            EXPORT_FOLDER=self.tmpdir,
            BACKUP_FOLDER=self.tmpdir,
        )
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        super().tearDown()

    def _make_file(self, name, age_seconds):
        path = os.path.join(self.tmpdir, name)
        with open(path, 'w') as f:
            f.write('x')
        old_time = time.time() - age_seconds
        os.utime(path, (old_time, old_time))
        return path

    def test_old_import_file_deleted(self):
        path = self._make_file('import_stale.csv', 7200)
        call_command('cleanup_temp_files', stdout=StringIO())
        self.assertFalse(os.path.exists(path))

    def test_fresh_import_file_kept(self):
        path = self._make_file('import_fresh.csv', 30)
        call_command('cleanup_temp_files', stdout=StringIO())
        self.assertTrue(os.path.exists(path))

    def test_unrecognized_prefix_is_never_deleted(self):
        path = self._make_file('random.csv', 999999)
        call_command('cleanup_temp_files', stdout=StringIO())
        self.assertTrue(os.path.exists(path))

    def test_safety_backup_has_longer_window(self):
        # Pre-restore backups are kept for a week, not an hour.
        path = self._make_file('pre_restore_old.db', 2 * 24 * 3600)
        call_command('cleanup_temp_files', stdout=StringIO())
        self.assertTrue(os.path.exists(path))


class RenewExpiredUsersTests(CacheClearingTestCase):
    def test_renews_eligible_users(self):
        u = make_user(
            'eligible',
            expires_at=timezone.now() - timedelta(days=1),
            auto_renew_days=30,
        )
        out = StringIO()
        call_command('renew_expired_users', stdout=out)
        u.refresh_from_db()
        self.assertGreater(u.expires_at, timezone.now())
        self.assertIn('1 users renewed', out.getvalue())

    def test_skips_admin_role(self):
        from tests.factories import make_admin
        admin = make_admin()
        admin.expires_at = timezone.now() - timedelta(days=1)
        admin.auto_renew_days = 30
        admin.save()
        original = admin.expires_at
        call_command('renew_expired_users', stdout=StringIO())
        admin.refresh_from_db()
        self.assertEqual(admin.expires_at, original)


class RefreshAuthorRanksTests(CacheClearingTestCase):
    def test_recomputes_trust_scores(self):
        from tests.factories import make_question
        author = make_user('author')
        make_question(owner=author, verified=True)
        make_question(owner=author, verified=False)
        out = StringIO()
        call_command('refresh_author_ranks', stdout=out)
        author.refresh_from_db()
        self.assertEqual(author.questions_count, 2)
        self.assertEqual(author.trust_score, 50.0)

    def test_quiet_suppresses_summary(self):
        out = StringIO()
        call_command('refresh_author_ranks', '--quiet', stdout=out)
        self.assertEqual(out.getvalue().strip(), '')


class SweepMasterExamsTests(CacheClearingTestCase):
    def test_no_expired_attempts_returns_zero(self):
        out = StringIO()
        call_command('sweep_master_exams', stdout=out)
        self.assertIn('0 attempt', out.getvalue())