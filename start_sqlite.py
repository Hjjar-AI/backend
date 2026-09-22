#!/usr/bin/env python3
"""Start a self-contained local Quiz instance with SQLite and LocMemCache.

Usage::

    python start_sqlite.py
    python start_sqlite.py 5005
    python start_sqlite.py localhost:5005
    python start_sqlite.py 0.0.0.0:8000
    python start_sqlite.py --seed-pro-users
    python start_sqlite.py --seed-pro-users --force-pro-users
    python start_sqlite.py --no-prompt
    SQLITE_DB_PATH=/tmp/quiz.sqlite3 python start_sqlite.py

ADDRESS PROMPT
--------------
When no address is passed on the command line, the launcher prompts
for one. Three input shapes are accepted:

  • Empty input       → ``localhost:5004`` (or ``SQLITE_SERVER_ADDRESS``
                        if set).
  • A bare port       → ``5005`` becomes ``localhost:5005``.
  • ``host:port``     → used as-is (e.g. ``0.0.0.0:8000``).

The prompt runs ONCE per launch, in the parent process. Django's
development auto-reloader spawns a child process with ``RUN_MAIN=true``
and re-executes this script; the parent rewrites ``sys.argv`` before
spawning so the child reads the already-resolved address as a plain
positional argument and never reaches the prompt a second time.

The prompt is skipped when stdin is not a terminal (CI, pipes,
``< /dev/null``) or when ``--no-prompt`` is passed.

WHAT THIS DOES
--------------
On every launch, in this order:

  1. Re-execs into a virtualenv if Django is not importable from the
     current interpreter.
  2. Asks for a server address when none was given on the command
     line and stdin is interactive.
  3. Creates ``backend/apps/__init__.py`` if missing.
  4. Creates a ``migrations/__init__.py`` inside every custom app
     that does not already have one.
  5. Runs ``makemigrations`` for any model-bearing app whose
     ``migrations/`` directory contains no migration files.
  6. Runs ``migrate`` with ``--fake-initial``.
  7. Runs the seed pipeline if no active superuser exists yet. The
     base seed is invoked as ``seed --only data --only capabilities``
     — the ``tips`` seeder is deliberately omitted.
  8. Optionally runs ``seed_pro_users`` via ``--seed-pro-users``.

Launcher flags (all optional):

  ``--seed-pro-users``     Run ``manage.py seed_pro_users`` during
                           startup. Idempotent — reports ``unchanged``
                           for accounts that already exist.

  ``--force-pro-users``    Also pass ``--reset-passwords`` to
                           ``seed_pro_users``. Implies
                           ``--seed-pro-users``.

  ``--no-setup``           Skip steps 3–7 and go straight to the
                           runserver.

  ``--no-prompt``          Do not prompt for the server address; use
                           the default silently. Implied when stdin
                           is not a terminal.

ENVIRONMENT KNOBS
-----------------
    SQLITE_DB_PATH          Path to the SQLite file.
                            Default: ``backend/SQLite/db.sqlite3``.
    SQLITE_SERVER_ADDRESS   Default address when nothing is passed on
                            the command line or at the prompt.
                            Default: ``localhost:5004``.
    SQLITE_AUTO_SETUP       Set to ``False``/``0``/``no``/``off`` to
                            behave as if ``--no-setup`` was passed.
    QUIZ_VENV               Path to a virtualenv to re-exec into when
                            the current interpreter cannot import
                            Django.

APP REGISTRY
------------
``CUSTOM_APPS``, ``MODEL_APPS``, and ``APP_INIT_CONTENT`` are imported
from ``apps.core.management._app_registry`` so this launcher and
``manage.py bootstrap`` share one source of truth. See that module's
docstring for the full rationale.

The import below runs BEFORE ``django.setup()`` — that is safe because
``_app_registry`` imports nothing from Django. A top-level Django
import there would raise ``AppRegistryNotReady``.
"""

import os
import sys
from importlib.util import find_spec
from pathlib import Path

# Ensure `backend/` is on sys.path so the shared app registry is
# importable. Running `python start_sqlite.py` from `backend/` (the
# documented invocation) already puts the script's directory at
# sys.path[0]; the explicit insert covers the case where the script
# is invoked from an unexpected working directory or via a launcher
# wrapper that resets sys.path.
_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from apps.core.management._app_registry import (  # noqa: E402
    APP_INIT_CONTENT,
    CUSTOM_APPS,
    MODEL_APPS,
)


_REQUIRED_MODULES = (
    'django',
    'jazzmin',
    'rest_framework',
    'corsheaders',
    'django_filters',
)


_SEED_ONLY = ('data', 'capabilities')


_LAUNCHER_FLAGS = {
    '--seed-pro-users': 'seed_pro_users',
    '--force-pro-users': 'force_pro_users',
    '--no-setup': 'no_setup',
    '--no-prompt': 'no_prompt',
}

_DEFAULT_HOST = 'localhost'
_DEFAULT_PORT = '5004'

# Django's autoreload module sets this environment variable in the
# child process it spawns for the actual worker. We use the same
# convention so our own "is this the reloader child?" check is
# indistinguishable from Django's.
_RELOADER_ENV_VAR = 'RUN_MAIN'


def _split_argv(argv):
    """
    Return (flags, positional_args).

    Every token matching a key in ``_LAUNCHER_FLAGS`` is a boolean
    launcher flag; everything else is collected in order into
    ``positional_args``, which the runserver will interpret as the
    bind address.
    """
    flags = {value: False for value in _LAUNCHER_FLAGS.values()}
    positional = []

    for token in argv:
        if token in _LAUNCHER_FLAGS:
            flags[_LAUNCHER_FLAGS[token]] = True
        elif token in ('--help', '-h'):
            _print_help()
            raise SystemExit(0)
        else:
            positional.append(token)

    if flags['force_pro_users']:
        flags['seed_pro_users'] = True

    return flags, positional


def _print_help():
    print(__doc__)


def _ensure_runtime():
    """Re-exec with the project virtualenv when dependencies are missing."""
    missing = [name for name in _REQUIRED_MODULES if find_spec(name) is None]
    if not missing:
        return

    backend_dir = Path(__file__).resolve().parent
    project_dir = backend_dir.parent
    candidates = []
    if os.environ.get('QUIZ_VENV'):
        candidates.append(Path(os.environ['QUIZ_VENV']).expanduser() / 'bin' / 'python')
    candidates.extend([
        project_dir / 'venv' / 'bin' / 'python',
        project_dir / '.venv' / 'bin' / 'python',
        Path.home() / 'Environments' / 'quizenv' / 'bin' / 'python',
    ])

    current = Path(sys.executable).absolute()
    for candidate in candidates:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        if candidate.absolute() == current:
            continue
        os.execv(
            str(candidate),
            [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]],
        )

    raise SystemExit(
        'Missing Python packages: '
        + ', '.join(missing)
        + '. Activate the Quiz virtual environment or set QUIZ_VENV.'
    )


def _enabled(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {'0', 'false', 'no', 'off'}


def _normalize_address(raw):
    """
    Turn a user-supplied address into a ``host:port`` string.

    Accepted shapes:

      • ``""``          → caller's default (handled by the caller).
      • ``"5005"``      → ``"localhost:5005"``.
      • ``":5005"``     → ``"localhost:5005"`` (empty host).
      • ``"host:5005"`` → returned unchanged.

    Anything not recognised as a port number when there is no colon is
    returned as-is and left for Django to reject with its own message.
    """
    value = raw.strip()
    if not value:
        return ''

    if value.isdigit():
        return f'{_DEFAULT_HOST}:{value}'

    if value.startswith(':'):
        tail = value[1:]
        if tail.isdigit():
            return f'{_DEFAULT_HOST}:{tail}'

    return value


def _resolve_server_address(positional_args, flags):
    """
    Return the list of arguments the runserver should receive after
    ``runserver``.

    Order of precedence:

      1. Anything passed on the command line, normalised through
         ``_normalize_address``.
      2. Interactive prompt, when stdin is a terminal and
         ``--no-prompt`` was not given.
      3. ``SQLITE_SERVER_ADDRESS`` environment variable.
      4. ``localhost:5004``.

    Only called from the parent process — see ``main`` for why.
    """
    default = os.environ.get(
        'SQLITE_SERVER_ADDRESS',
        f'{_DEFAULT_HOST}:{_DEFAULT_PORT}',
    )

    if positional_args:
        head = _normalize_address(positional_args[0]) or default
        return [head, *positional_args[1:]]

    if flags['no_prompt'] or not sys.stdin.isatty():
        return [default]

    print('')
    print('Server address')
    print(f'  Enter             → default [{default}]')
    print(f'  A port number     → {_DEFAULT_HOST}:<port>   (e.g. 5005)')
    print('  host:port         → used as-is    (e.g. 0.0.0.0:8000)')
    print('')

    try:
        answer = input('> ').strip()
    except (EOFError, KeyboardInterrupt):
        print('')
        answer = ''

    print('')
    normalized = _normalize_address(answer)
    return [normalized if normalized else default]


def _ensure_package_files(backend_dir):
    """
    Create ``apps/__init__.py`` and one ``migrations/__init__.py`` per
    custom app if any of them are missing.

    MUST run before ``django.setup()``.
    """
    apps_dir = backend_dir / 'apps'

    apps_init = apps_dir / '__init__.py'
    if not apps_init.exists():
        apps_init.write_text(APP_INIT_CONTENT, encoding='utf-8')
        print(f'[setup] created {apps_init.relative_to(backend_dir)}')

    for app in CUSTOM_APPS:
        app_dir = apps_dir / app
        if not app_dir.is_dir():
            continue
        mig_dir = app_dir / 'migrations'
        mig_dir.mkdir(exist_ok=True)
        mig_init = mig_dir / '__init__.py'
        if not mig_init.exists():
            mig_init.write_text('', encoding='utf-8')
            print(f'[setup] created {mig_init.relative_to(backend_dir)}')


def _app_needs_makemigrations(backend_dir, app):
    mig_dir = backend_dir / 'apps' / app / 'migrations'
    if not mig_dir.is_dir():
        return True
    for entry in mig_dir.iterdir():
        if entry.is_file() and entry.suffix == '.py' and entry.name != '__init__.py':
            return False
    return True


def main():
    flags, positional = _split_argv(sys.argv[1:])

    _ensure_runtime()

    if os.environ.get(_RELOADER_ENV_VAR) == 'true':
        # Child process. Do not prompt, do not run setup.
        runserver_args = positional or [
            os.environ.get(
                'SQLITE_SERVER_ADDRESS',
                f'{_DEFAULT_HOST}:{_DEFAULT_PORT}',
            )
        ]
    else:
        # Parent process. Resolve the address once, then rewrite
        # sys.argv so the child inherits it.
        runserver_args = _resolve_server_address(positional, flags)
        sys.argv = [sys.argv[0], *runserver_args]

    backend_dir = Path(__file__).resolve().parent

    os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings_sqlite'

    auto_setup = (
        _enabled('SQLITE_AUTO_SETUP')
        and not flags['no_setup']
    )

    if auto_setup:
        _ensure_package_files(backend_dir)

    try:
        import django
        from django.conf import settings
        from django.core.management import call_command, execute_from_command_line
    except ImportError as exc:
        raise SystemExit(
            "Django is unavailable. Activate the Quiz virtual environment "
            "and run this launcher again."
        ) from exc

    django.setup()

    database_path = settings.DATABASES['default']['NAME']
    print(f'Using SQLite database: {database_path}')
    print('Using cache: LocMemCache (single process)')

    # Setup steps run only in the parent. The child re-exec reached
    # this point with RUN_MAIN=true and would otherwise re-run every
    # step on every code save.
    is_parent = os.environ.get(_RELOADER_ENV_VAR) != 'true'

    if is_parent and auto_setup:
        needs = [app for app in MODEL_APPS
                 if _app_needs_makemigrations(backend_dir, app)]
        if needs:
            print(f'[setup] generating initial migrations for: {", ".join(needs)}')
            call_command('makemigrations', *needs)

        print('Applying pending migrations...')
        call_command('migrate', interactive=False, fake_initial=True)

        from django.contrib.auth import get_user_model
        User = get_user_model()

        try:
            has_superuser = User.objects.filter(
                is_superuser=True, is_active=True,
            ).exists()
        except Exception as exc:
            print('')
            print('ERROR: could not query the users table.')
            print(f'  {type(exc).__name__}: {exc}')
            print('')
            print('Check that:')
            print('  1. backend/apps/__init__.py exists (empty file).')
            print('  2. Each custom app has a migrations/ directory')
            print('     with an empty __init__.py.')
            print('  3. `manage.py showmigrations users` lists at least')
            print('     the 0001_initial migration.')
            print('')
            print('If a partial database was created by an earlier run,')
            print('delete it and retry:  rm -f backend/SQLite/db.sqlite3')
            raise SystemExit(1)

        if not has_superuser:
            print('No active superuser found — running the seed pipeline...')
            print(f'  (skipping tips; run `manage.py seed --only tips` '
                  f'later if needed)')
            print('(The admin password will be printed to stderr below.)')

            # `seed` is the orchestrator command; it owns the
            # canonical ordering of the individual seeders. Passing
            # `only` narrows its selection to the two we want.
            call_command('seed', only=list(_SEED_ONLY))

            print('')
            print('Seeding complete. Use the credentials printed above to log in.')
        else:
            print('Superuser already present — skipping base seed.')

        if flags['seed_pro_users']:
            kwargs = {}
            if flags['force_pro_users']:
                kwargs['reset_passwords'] = True
                print('[setup] running seed_pro_users --reset-passwords...')
            else:
                print('[setup] running seed_pro_users...')
            call_command('seed_pro_users', **kwargs)

    elif is_parent and flags['seed_pro_users']:
        kwargs = {}
        if flags['force_pro_users']:
            kwargs['reset_passwords'] = True
        print('[setup] running seed_pro_users (setup otherwise skipped)...')
        call_command('seed_pro_users', **kwargs)

    execute_from_command_line([
        sys.argv[0],
        'runserver',
        *runserver_args,
    ])


if __name__ == '__main__':
    main()