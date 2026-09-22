# tests/management/test_seeders.py
"""
The individual seeder commands. `seed` and `seed_capabilities`
have their own file; this one covers the four seeders that create
content and the security-sensitive admin password reset.

Every seeder is idempotent — the tests verify that explicitly,
because a seeder that double-creates on a re-run is the classic
"works the first time, corrupts the second time" bug.

STDERR CAPTURE
--------------
`call_command(..., stderr=buf)` captures writes made through the
command's own `self.stderr` handle. It does NOT capture writes made
through `sys.stderr.write(...)` directly. `reset_admin_password`
uses `sys.stderr` directly for the one-time credential output (a
deliberate choice: the command does not want the credential written
to the log handler that may be attached to the command's stream).
The tests that verify the credential output use
`contextlib.redirect_stderr` to capture it.

ANSI COLOUR CODES
-----------------
`self.style.SUCCESS(...)` wraps its argument in ANSI escape codes.
Substring assertions on the message text still work because the
codes are on the ends, not interspersed.
"""
import os
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command

from apps.core.models import Setting, Tip
from apps.questions.models import Question, Category, ClinicalCase, Tag
from apps.users.models import User
from tests.base import CacheClearingTestCase
from tests.factories import make_admin


class SeedDataTests(CacheClearingTestCase):
    def test_first_run_creates_admin(self):
        call_command('seed_data', stdout=StringIO())
        admin = User.objects.get(username='admin')
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertEqual(admin.role, 'admin')
        self.assertTrue(admin.must_change_password)

    def test_first_run_creates_categories(self):
        call_command('seed_data', stdout=StringIO())
        self.assertEqual(Category.objects.count(), 18)

    def test_first_run_creates_settings(self):
        call_command('seed_data', stdout=StringIO())
        self.assertEqual(
            Setting.objects.get(key='default_expiry_days').value, '60',
        )
        self.assertEqual(
            Setting.objects.get(key='default_renewal_days').value, '30',
        )
        self.assertEqual(
            Setting.objects.get(key='exam_duration_minutes').value, '60',
        )

    def test_second_run_is_idempotent_for_admin(self):
        call_command('seed_data', stdout=StringIO())
        admin_pk = User.objects.get(username='admin').pk

        out = StringIO()
        call_command('seed_data', stdout=out)
        self.assertIn('Admin already exists', out.getvalue())
        self.assertEqual(User.objects.get(username='admin').pk, admin_pk)

    def test_second_run_does_not_duplicate_categories(self):
        call_command('seed_data', stdout=StringIO())
        first_count = Category.objects.count()
        call_command('seed_data', stdout=StringIO())
        self.assertEqual(Category.objects.count(), first_count)

    def test_admin_password_from_env_is_used(self):
        with patch.dict(os.environ, {'ADMIN_PASSWORD': 'env-supplied-pw-99'}):
            call_command('seed_data', stdout=StringIO())
        admin = User.objects.get(username='admin')
        self.assertTrue(admin.check_password('env-supplied-pw-99'))


class SeedTipsTests(CacheClearingTestCase):
    def test_creates_both_locales(self):
        call_command('seed_tips', stdout=StringIO())
        self.assertGreater(Tip.objects.filter(locale='ar').count(), 0)
        self.assertGreater(Tip.objects.filter(locale='en').count(), 0)

    def test_second_run_is_idempotent(self):
        """
        The success line is a single sentence:
        "Added N Arabic tips and M English tips." — one string, not
        two. Assert against the combined message.
        """
        call_command('seed_tips', stdout=StringIO())
        first_total = Tip.objects.count()

        out = StringIO()
        call_command('seed_tips', stdout=out)
        self.assertIn('Added 0 Arabic tips and 0 English tips', out.getvalue())
        self.assertEqual(Tip.objects.count(), first_total)


class SeedSampleQuestionsTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_admin('admin_a', 'admin-pw-1234')

    def test_creates_all_sample_questions(self):
        call_command('seed_sample_questions', stdout=StringIO())
        # 10 standalone + 3 depression chain + 3 psychosis chain = 16
        self.assertEqual(Question.objects.count(), 16)

    def test_creates_both_clinical_cases(self):
        call_command('seed_sample_questions', stdout=StringIO())
        self.assertTrue(
            ClinicalCase.objects.filter(key='case-depression-01').exists(),
        )
        self.assertTrue(
            ClinicalCase.objects.filter(key='case-psychosis-01').exists(),
        )

    def test_case_order_is_sequential(self):
        call_command('seed_sample_questions', stdout=StringIO())
        for key in ('case-depression-01', 'case-psychosis-01'):
            orders = list(
                Question.objects
                .filter(case__key=key)
                .order_by('case_order')
                .values_list('case_order', flat=True)
            )
            self.assertEqual(orders, [1, 2, 3], f'case {key}')

    def test_owned_by_admin_authored_by_null(self):
        call_command('seed_sample_questions', stdout=StringIO())
        for q in Question.objects.all():
            self.assertIsNone(q.authored_by)
            self.assertEqual(q.owned_by, self.admin)

    def test_questions_marked_verified(self):
        call_command('seed_sample_questions', stdout=StringIO())
        self.assertEqual(
            Question.objects.filter(verified=True).count(),
            Question.objects.count(),
        )

    def test_second_run_is_idempotent(self):
        call_command('seed_sample_questions', stdout=StringIO())
        first_count = Question.objects.count()

        out = StringIO()
        call_command('seed_sample_questions', stdout=out)
        self.assertIn('Added 0 sample questions', out.getvalue())
        self.assertEqual(Question.objects.count(), first_count)

    def test_requires_an_owner(self):
        self.admin.delete()
        out = StringIO()
        err = StringIO()
        call_command(
            'seed_sample_questions', stdout=out, stderr=err,
        )
        self.assertIn('no active admin', err.getvalue())
        self.assertEqual(Question.objects.count(), 0)

    def test_tags_are_created_and_linked(self):
        call_command('seed_sample_questions', stdout=StringIO())
        self.assertGreater(Tag.objects.count(), 0)
        tagged = Question.objects.filter(tags__isnull=False).distinct()
        self.assertGreater(tagged.count(), 0)

    def test_categories_are_linked(self):
        call_command('seed_data', stdout=StringIO())
        call_command('seed_sample_questions', stdout=StringIO())
        linked = Question.objects.filter(category__isnull=False)
        self.assertGreater(linked.count(), 0)


class ResetAdminPasswordTests(CacheClearingTestCase):
    """
    The command prints a one-time password to `sys.stderr` directly
    (not through `self.stderr`), and sets must_change_password=True
    so the credential cannot linger.
    """

    def test_no_admin_prints_error_and_exits(self):
        # The "Admin user not found" message goes through
        # `self.stderr.write`, so `call_command(stderr=...)` captures it.
        err = StringIO()
        call_command('reset_admin_password', stderr=err)
        self.assertIn('Admin user not found', err.getvalue())

    def test_resets_password_and_sets_flag(self):
        admin = make_admin('admin', 'original-pw-1234')
        admin.must_change_password = False
        admin.save()

        with redirect_stderr(StringIO()):
            call_command('reset_admin_password')

        admin.refresh_from_db()
        self.assertTrue(admin.must_change_password)
        self.assertFalse(admin.check_password('original-pw-1234'))

    def test_prints_new_password_to_stderr(self):
        make_admin('admin', 'original-pw-1234')
        buf = StringIO()
        with redirect_stderr(buf):
            call_command('reset_admin_password')
        self.assertIn('New Password:', buf.getvalue())
        self.assertIn('Username: admin', buf.getvalue())

    def test_qr_flag_does_not_crash_without_library(self):
        """
        The command tries `import qrcode` and falls back to a
        message on stderr if the import fails. Either way, the
        command succeeds and prints the credential.
        """
        make_admin('admin', 'original-pw-1234')
        buf = StringIO()
        with redirect_stderr(buf):
            call_command('reset_admin_password', '--qr')
        self.assertIn('New Password:', buf.getvalue())