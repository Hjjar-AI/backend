# backend/apps/core/management/commands/seed_capabilities.py
"""
Seed / sync the RoleCapabilities table from the code constants.

Idempotent. Safe to run on every deploy.

Modes
-----
Default
    Create missing rows. Add any new capabilities that the seed
    defines but the row does not have. Never remove capabilities
    already granted in the DB — those are treated as deliberate panel
    edits. Also drop any stored capability string that no longer
    exists in CAPABILITIES, so a removed capability cannot linger in
    a row.

--reset
    Overwrite every row with the seed constants. Use this when you
    want to discard panel edits and return to code-defined defaults.

Note
----
An earlier revision of this command backfilled the deprecated
per-user master-exam authoring boolean into a capability override.
That boolean has been removed from the User model (Phase 5 of the
permission migration) and the one-time backfill now lives inside
the field-removal migration itself, so this command no longer
touches User rows.

Usage
-----
    python manage.py seed_capabilities
    python manage.py seed_capabilities --reset
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.users.capabilities import (
    CAPABILITIES,
    DEFAULT_ROLE_CAPABILITIES,
)
from apps.users.models import RoleCapabilities
from apps.users.services.permission_service import (
    invalidate_role_capabilities,
)


class Command(BaseCommand):
    help = 'Seed or sync RoleCapabilities from DEFAULT_ROLE_CAPABILITIES.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset',
            action='store_true',
            help=(
                'Overwrite every RoleCapabilities row with the code '
                'defaults. Discards panel edits.'
            ),
        )
        parser.add_argument(
            '--quiet', '-q',
            action='store_true',
            help='Suppress the summary output on success.',
        )

    def handle(self, *args, **options):
        reset = options['reset']
        quiet = options['quiet']

        created, updated, unchanged = self._sync_roles(reset=reset)

        # Any role change invalidates the shared cache. Do it once at
        # the end rather than per-row so the panel sees a consistent
        # view immediately after the command exits.
        invalidate_role_capabilities()

        if quiet:
            return

        self.stdout.write(f'Roles created:   {created}')
        self.stdout.write(f'Roles updated:   {updated}')
        self.stdout.write(f'Roles unchanged: {unchanged}')
        self.stdout.write(self.style.SUCCESS('Capabilities seeded.'))

    @transaction.atomic
    def _sync_roles(self, reset):
        created = updated = unchanged = 0

        for role, seed_caps in DEFAULT_ROLE_CAPABILITIES.items():
            seed_list = sorted(seed_caps)
            row = RoleCapabilities.objects.filter(role=role).first()

            if row is None:
                RoleCapabilities.objects.create(
                    role=role,
                    capabilities=seed_list,
                )
                created += 1
                continue

            existing = list(row.capabilities or [])

            if reset:
                if existing != seed_list:
                    row.capabilities = seed_list
                    row.save(update_fields=['capabilities', 'updated_at'])
                    updated += 1
                else:
                    unchanged += 1
                continue

            # Merge mode.
            #   • Add capabilities the seed defines that the row lacks.
            #   • Drop anything no longer in CAPABILITIES so the row
            #     cannot grant a capability that has been removed from
            #     the code.
            known = [c for c in existing if c in CAPABILITIES]
            missing = [c for c in seed_list if c not in existing]

            if missing or len(known) != len(existing):
                merged = sorted(set(known) | set(missing))
                row.capabilities = merged
                row.save(update_fields=['capabilities', 'updated_at'])
                updated += 1
            else:
                unchanged += 1

        return created, updated, unchanged