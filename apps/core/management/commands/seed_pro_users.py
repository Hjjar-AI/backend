# backend/apps/core/management/commands/seed_pro_users.py
"""
Seed moderator accounts for the doctors credited on the About page.

Usage:
    python manage.py seed_pro_users
    python manage.py seed_pro_users --reset-passwords
    python manage.py seed_pro_users --quiet

WHAT IT CREATES
---------------
One active moderator account per doctor listed in the About page's
"Testing and verification team" section. Accounts are matched by
username, so re-running the command is idempotent: an existing
account is left alone unless --reset-passwords is passed.

PASSWORD SOURCE
---------------
Every account is seeded with the same one-time password, read from
the SEED_PRO_USER_PASSWORD environment variable (or the same key in
backend/.env, loaded by settings.py). The default when that variable
is not set is `1234test`.

This value is a HAND-OUT CREDENTIAL, not a long-lived password.
Every account created or reset by this command is marked
`must_change_password=True`, and MustChangePasswordMiddleware blocks
everything except the change-password flow until that flag is
cleared. The account holder replaces the value on first login.

Prefer SEED_PRO_USER_PASSWORD in backend/.env over an inline
default: the .env file is 0600 and is read from a known location,
whereas a hardcoded default is visible to anyone who can read this
source file.

WHY must_change_password=True (fix — previous revision set it False)
--------------------------------------------------------------------
The previous revision seeded these accounts with
`must_change_password=False` and per-user passwords of the form
`firstname + "12345"`. Both halves of that decision were wrong for a
moderator account that has `questions.edit_any`,
`questions.verify`, and `groups.admin` capabilities:

  • A per-user password derived from a name that appears on the
    public About page is guessable.
  • The flag being False meant the credential never rotated unless
    someone ran `--reset-passwords` or manually edited the account.

The flag is now True, matching `reset_admin_password` and
`seed_data.seed_admin` — every other one-time credential path in
this codebase already sets it.
"""

import os
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


User = get_user_model()


# Username → display names. No password column — the seeded password
# is a single value shared across every account and sourced from
# SEED_PRO_USER_PASSWORD at run time.
#
# Usernames are latinised first names. They satisfy the User model's
# USERNAME_REGEX (letters, digits, underscores, 3-50 chars).
#
# Order matches the About page's Arabic alphabetical listing.
PRO_USERS = [
    ('aya_kseibi',        'د. آية كسيبي',              'Dr. Aya Kseibi'),
    ('ayham_shaykha',     'د. أيهم شيخة',              'Dr. Ayham Shaykha'),
    ('ibrahim_tarsha',    'د. إبراهيم طرشه',           'Dr. Ibrahim Tarsha'),
    ('shvan_shamsi',      'د. شفان شمسي',              'Dr. Shvan Shamsi'),
    ('zilal_alwaw',       'د. ظلال الواو',             'Dr. Zilal Al-Waw'),
    ('nidal_abdulwahhab', 'د. محمد نضال عبد الوهاب',   'Dr. Muhammad Nidal Abdul-Wahhab'),
    ('nour_alsayed',      'د. محمد نور السيد',         'Dr. Muhammad Nour Al-Sayed'),
    ('hadiyatullah_malas','د. هدية الله ملص',          'Dr. Hadiyatullah Malas'),
]


# Default one-time password when SEED_PRO_USER_PASSWORD is not set.
# Every account seeded with this value is required to change it on
# first login via `must_change_password=True` below.
_DEFAULT_SEED_PASSWORD = '1234test'


DEFAULTS = {
    'role': 'moderator',
    'is_active': True,
    'is_staff': False,
    'is_superuser': False,
    'is_stub': False,
    # One-time credential — the middleware gates the session until
    # the password is changed. See the module docstring for why this
    # is True.
    'must_change_password': True,
    'auto_renew_days': 0,
    'expires_at': None,
}


class Command(BaseCommand):
    help = (
        'Seed moderator accounts for the pro/verifying doctors listed '
        'on the About page. Idempotent by username.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset-passwords',
            action='store_true',
            help=(
                'Reset the password on existing accounts to the seeded '
                'value (from SEED_PRO_USER_PASSWORD, or the built-in '
                'default). Without this flag, an existing account is '
                'left untouched — only its role is corrected if it '
                'drifted.'
            ),
        )
        parser.add_argument(
            '--quiet', '-q',
            action='store_true',
            help='Suppress the summary line on success (for CI/cron).',
        )

    def handle(self, *args, **options):
        reset_passwords = options['reset_passwords']
        quiet = options['quiet']

        # Resolve the shared one-time password once. Prefer the .env
        # variable because a default literal in source is visible to
        # anyone with read access to this file.
        seed_password = (
            os.environ.get('SEED_PRO_USER_PASSWORD')
            or _DEFAULT_SEED_PASSWORD
        )

        created = 0
        updated = 0
        unchanged = 0
        credentials = []

        for username, full_name_ar, full_name_en in PRO_USERS:
            user, was_created = User.objects.get_or_create(
                username=username,
                defaults={
                    'full_name': full_name_ar,
                    **DEFAULTS,
                },
            )

            if was_created:
                user.set_password(seed_password)
                # Belt-and-braces: DEFAULTS already sets this, but
                # set it again explicitly so a future change to
                # DEFAULTS cannot silently drop the flag on the
                # create path.
                user.must_change_password = True
                user.save(update_fields=['password', 'must_change_password'])
                created += 1
                credentials.append((username, full_name_en, seed_password))
                continue

            # Existing account. Correct role and full_name if they
            # drifted; do not touch the password unless asked.
            dirty_fields = []
            if user.role != DEFAULTS['role']:
                user.role = DEFAULTS['role']
                dirty_fields.append('role')
            if not user.is_active:
                user.is_active = True
                dirty_fields.append('is_active')
            # Prefer the English full name if the current one is empty;
            # otherwise leave the admin's chosen display name alone.
            if not user.full_name:
                user.full_name = full_name_ar
                dirty_fields.append('full_name')

            if reset_passwords:
                user.set_password(seed_password)
                # A reset implies the new credential is also one-time.
                # Set the flag True regardless of its prior state.
                user.must_change_password = True
                dirty_fields.extend(['password', 'must_change_password'])
                credentials.append((username, full_name_en, seed_password))

            if dirty_fields:
                user.save(update_fields=dirty_fields)
                updated += 1
            else:
                unchanged += 1

        # The admin asked for hand-out credentials — print them on
        # stderr so they are not interleaved with the command's
        # stdout summary and can be piped separately if desired.
        for username, full_name, password in credentials:
            sys.stderr.write(f'{username:<22} {password:<22}  # {full_name}\n')

        if quiet:
            return

        self.stdout.write(self.style.SUCCESS(
            f'Pro users: {created} created, {updated} updated, '
            f'{unchanged} unchanged.'
        ))
        if credentials:
            self.stdout.write(
                f'Printed {len(credentials)} credential line(s) to stderr. '
                f'Every account carries must_change_password=True and will '
                f'be required to change the password on first login.'
            )
        else:
            self.stdout.write(
                'No passwords printed. Pass --reset-passwords to reset '
                'existing accounts.'
            )