# tests/management/test_seed_commands.py
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command

from apps.users.capabilities import (
    CAPABILITIES, DEFAULT_ROLE_CAPABILITIES,
)
from apps.users.models import RoleCapabilities
from apps.users.services.permission_service import role_capabilities
from tests.base import CacheClearingTestCase


class SeedCapabilitiesCommandTests(CacheClearingTestCase):
    def test_first_run_creates_all_roles(self):
        out = StringIO()
        call_command('seed_capabilities', stdout=out)
        for role in ('member', 'moderator', 'admin'):
            self.assertTrue(
                RoleCapabilities.objects.filter(role=role).exists(),
                f'role {role} not created',
            )

    def test_second_run_is_idempotent(self):
        call_command('seed_capabilities', stdout=StringIO())
        out = StringIO()
        call_command('seed_capabilities', stdout=out)
        self.assertIn('Roles unchanged: 3', out.getvalue())

    def test_merge_mode_adds_missing_caps(self):
        call_command('seed_capabilities', stdout=StringIO())
        row = RoleCapabilities.objects.get(role='member')
        # Strip one capability.
        row.capabilities = [
            c for c in row.capabilities if c != 'questions.create'
        ]
        row.save()

        call_command('seed_capabilities', stdout=StringIO())
        row.refresh_from_db()
        self.assertIn('questions.create', row.capabilities)

    def test_merge_mode_drops_unknown_caps(self):
        call_command('seed_capabilities', stdout=StringIO())
        row = RoleCapabilities.objects.get(role='member')
        row.capabilities = list(row.capabilities) + ['ghost.cap']
        row.save()

        call_command('seed_capabilities', stdout=StringIO())
        row.refresh_from_db()
        self.assertNotIn('ghost.cap', row.capabilities)

    def test_reset_mode_overwrites_panel_edits(self):
        call_command('seed_capabilities', stdout=StringIO())
        row = RoleCapabilities.objects.get(role='member')
        # A panel edit that ADDED a cap not in defaults.
        row.capabilities = sorted(
            set(row.capabilities) | {'questions.verify'},
        )
        row.save()

        call_command('seed_capabilities', '--reset', stdout=StringIO())
        row.refresh_from_db()
        self.assertNotIn('questions.verify', row.capabilities)
        self.assertEqual(
            set(row.capabilities), DEFAULT_ROLE_CAPABILITIES['member'],
        )

    def test_command_invalidates_cache(self):
        # Prime the cache.
        role_capabilities('member')
        call_command('seed_capabilities', stdout=StringIO())
        # No explicit invalidate — the command must have done it.
        cached = role_capabilities('member')
        self.assertEqual(
            cached, set(DEFAULT_ROLE_CAPABILITIES['member']),
        )


class SeedWrapperCommandTests(CacheClearingTestCase):
    """
    The `seed` wrapper's job is dispatch order, not seeding. Mock
    call_command to verify order and set membership without running
    the real seeders.
    """
    def test_list_prints_all_seeders(self):
        out = StringIO()
        call_command('seed', '--list', stdout=out)
        content = out.getvalue()
        for name in ('data', 'tips', 'capabilities', 'questions'):
            self.assertIn(name, content)

    def test_default_runs_data_tips_capabilities(self):
        with patch('apps.core.management.commands.seed.call_command') as mock:
            call_command('seed', stdout=StringIO())
        called = [c.args[0] for c in mock.call_args_list]
        self.assertEqual(called, ['seed_data', 'seed_tips', 'seed_capabilities'])

    def test_with_sample_questions_appends_questions_seeder(self):
        with patch('apps.core.management.commands.seed.call_command') as mock:
            call_command('seed', '--with-sample-questions', stdout=StringIO())
        called = [c.args[0] for c in mock.call_args_list]
        self.assertEqual(called, [
            'seed_data', 'seed_tips', 'seed_capabilities',
            'seed_sample_questions',
        ])

    def test_only_restricts_to_named_seeder(self):
        with patch('apps.core.management.commands.seed.call_command') as mock:
            call_command('seed', '--only', 'capabilities', stdout=StringIO())
        called = [c.args[0] for c in mock.call_args_list]
        self.assertEqual(called, ['seed_capabilities'])

    def test_only_preserves_canonical_order(self):
        with patch('apps.core.management.commands.seed.call_command') as mock:
            call_command(
                'seed',
                '--only', 'capabilities',
                '--only', 'data',
                stdout=StringIO(),
            )
        called = [c.args[0] for c in mock.call_args_list]
        # data comes before capabilities in _SEEDER_ORDER regardless
        # of the order the caller listed them.
        self.assertEqual(called, ['seed_data', 'seed_capabilities'])

    def test_reset_flag_forwarded_to_capabilities(self):
        with patch('apps.core.management.commands.seed.call_command') as mock:
            call_command(
                'seed', '--only', 'capabilities', '--reset',
                stdout=StringIO(),
            )
        # The single call should carry reset=True.
        self.assertEqual(mock.call_count, 1)
        kwargs = mock.call_args_list[0].kwargs
        self.assertTrue(kwargs.get('reset'))

    def test_quiet_suppresses_output(self):
        out = StringIO()
        with patch('apps.core.management.commands.seed.call_command'):
            call_command('seed', '--quiet', stdout=out)
        self.assertEqual(out.getvalue().strip(), '')