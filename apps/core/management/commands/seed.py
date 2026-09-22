# backend/apps/core/management/commands/seed.py
"""
Orchestrator for the independent seed commands.

This command does NOT contain seed data or logic itself. It is a
thin wrapper that dispatches to:

    seed_data                — admin user, categories, settings
    seed_tips                — tip strings (ar + en)
    seed_capabilities        — RoleCapabilities rows
    seed_sample_questions    — sample questions + clinical cases

...each of which remains a standalone command and can still be run
directly. Keeping them separate means:

  • The data-heavy files (seed_tips.py in particular, with its
    ~200 strings) are edited in isolation, not inside a master file.
  • A developer working on one seeder does not have to page through
    the others.
  • The commands remain individually testable and individually
    runnable in a deployment that only needs one of them.

WHY THE WRAPPER EXISTS
----------------------
`bootstrap` needs to run three of the four seeders in a specific
order. Before this wrapper, bootstrap called each command by name.
Adding a new seeder meant editing bootstrap. Now bootstrap calls
`seed` and the wrapper owns the ordering.

Adding a new seeder:
  1. Create `seed_<name>.py` as a normal management command.
  2. Add an entry to `_SEEDER_ORDER` below.
  3. Nothing else changes.

DEFAULT SET VS. OPT-IN
----------------------
`seed_sample_questions` seeds demo content (the sample psychiatry
bank). It is idempotent, but a production deploy that never touches
it should not have it injected into the database on every bootstrap.
It is therefore OPT-IN: the wrapper's default set is data + tips +
capabilities, and the questions seeder runs only when explicitly
requested via `--only questions` or `--with-sample-questions`.

Usage
-----
    python manage.py seed                     # data + tips + capabilities
    python manage.py seed --with-sample-questions
    python manage.py seed --only capabilities
    python manage.py seed --only tips --only questions
    python manage.py seed --reset             # forwarded to capabilities
    python manage.py seed --quiet             # suppress per-seeder output
    python manage.py seed --list              # print the available seeder names
"""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import BaseCommand


# The canonical set of seed commands this wrapper knows how to
# dispatch. Order matters — later seeders may depend on rows created
# by earlier ones:
#
#   • seed_sample_questions references categories by name, so
#     seed_data must run first.
#   • seed_capabilities is independent but conventionally runs
#     before any content is added (so the admin's panel reflects
#     the seeded role set immediately).
_SEEDER_ORDER = (
    ('data', 'seed_data'),
    ('tips', 'seed_tips'),
    ('capabilities', 'seed_capabilities'),
    ('questions', 'seed_sample_questions'),
)

_AVAILABLE = {key for key, _ in _SEEDER_ORDER}

# The subset that runs when no --only / --with-sample-questions is
# given. Excludes 'questions' on purpose — see the module docstring.
_DEFAULT_SEEDERS = ('data', 'tips', 'capabilities')


class Command(BaseCommand):
    help = (
        'Run one or more seeders in order. Wraps seed_data, seed_tips, '
        'seed_capabilities, and seed_sample_questions.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--only',
            action='append',
            choices=sorted(_AVAILABLE),
            dest='only',
            metavar='NAME',
            help=(
                'Run only the named seeder. May be repeated. '
                f'Choices: {", ".join(sorted(_AVAILABLE))}. '
                'Without --only, the default set '
                f'({", ".join(_DEFAULT_SEEDERS)}) runs.'
            ),
        )
        parser.add_argument(
            '--with-sample-questions',
            action='store_true',
            dest='with_sample_questions',
            help=(
                'Also run seed_sample_questions. Equivalent to '
                'adding `--only questions`. No effect if `questions` '
                'is already in the selection.'
            ),
        )
        parser.add_argument(
            '--reset',
            action='store_true',
            help=(
                'Forwarded to seed_capabilities: overwrite every '
                'RoleCapabilities row with the code defaults, '
                'discarding panel edits. No effect on the other '
                'seeders (they are all idempotent).'
            ),
        )
        parser.add_argument(
            '--quiet', '-q',
            action='store_true',
            help='Suppress per-seeder output.',
        )
        parser.add_argument(
            '--list',
            action='store_true',
            help='Print the available seeder names and exit.',
        )

    def handle(self, *args, **options):
        if options['list']:
            for key, cmd in _SEEDER_ORDER:
                default_marker = '' if key in _DEFAULT_SEEDERS else '  (opt-in)'
                self.stdout.write(
                    f'  {key:<12} -> manage.py {cmd}{default_marker}'
                )
            return

        # ── Build the run set ──────────────────────────────────────
        selected = set(options.get('only') or _DEFAULT_SEEDERS)
        if options.get('with_sample_questions'):
            selected.add('questions')

        # Preserve the canonical order regardless of how the user
        # listed --only names. Sorting by the canonical index gives
        # the same ordering that bootstrap has always used, and lets
        # `--only questions --only data` produce the same run as
        # `--only data --only questions`.
        order_map = {key: idx for idx, (key, _) in enumerate(_SEEDER_ORDER)}
        selected = sorted(selected, key=lambda k: order_map[k])

        quiet = options['quiet']
        forward_reset = options.get('reset', False)

        # ── Dispatch ───────────────────────────────────────────────
        cmd_lookup = dict(_SEEDER_ORDER)
        for key in selected:
            cmd_name = cmd_lookup[key]

            if not quiet:
                self.stdout.write(f'[seed] running {cmd_name}...')

            # Flag forwarding. Only seed_capabilities accepts an
            # extra flag today; the branch is written so a second
            # flag on any seeder is a one-line addition here rather
            # than a rewrite of a nested loop.
            forwarded = {}
            if key == 'capabilities' and forward_reset:
                forwarded['reset'] = True

            # Route each seeder's output through a throwaway buffer
            # when --quiet is set. This is preferred over forwarding
            # a `quiet` flag because not every seeder defines one
            # (seed_data / seed_tips / seed_sample_questions have no
            # `--quiet` option, and passing an unknown kwarg to
            # call_command raises TypeError). Capturing stdout is
            # equivalent to silencing and works for every seeder.
            if quiet:
                sink_out = StringIO()
                sink_err = StringIO()
                call_command(cmd_name, stdout=sink_out, stderr=sink_err, **forwarded)
            else:
                call_command(cmd_name, **forwarded)

        if not quiet:
            plural = 's' if len(selected) != 1 else ''
            self.stdout.write(self.style.SUCCESS(
                f'[seed] complete ({len(selected)} seeder{plural} run)'
            ))