#!/usr/bin/env python
# tests/runtests.py
"""
Test runner for the backend Django project.

WHY THIS EXISTS
---------------
This shim selects the self-contained test settings, initializes
Django, and hands off to Django's standard DiscoverRunner.

DEFAULT SETTINGS
----------------
If DJANGO_SETTINGS_MODULE is not already set in the environment,
this script sets it to `tests.test_settings`, which runs the suite
against in-memory SQLite with locmem cache and MD5 password hashing.
That means:

    python tests/runtests.py

...works on a fresh clone with no MySQL credentials and no
`test_quiz` database provisioning. To run against MariaDB instead:

    DJANGO_SETTINGS_MODULE=config.settings python tests/runtests.py

USAGE
-----
    python tests/runtests.py                    # everything
    python tests/runtests.py --failfast         # stop on first failure
    python tests/runtests.py users              # one package
    python tests/runtests.py users.test_authentication
    python tests/runtests.py questions analytics  # multiple packages
"""
import os
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parent.parent


if not (BACKEND / 'manage.py').is_file():
    sys.stderr.write(
        f'Cannot find backend/ at {BACKEND}. '
        f'This runner must live under the backend project.\n'
    )
    sys.exit(2)

# The backend directory contains `config`, `apps`, and `tests`.
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


# Default to the SQLite overlay. Respect an explicit choice from the
# environment (config.settings, or any future module).
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tests.test_settings')


import django  # noqa: E402
from django.conf import settings  # noqa: E402
from django.test.utils import get_runner  # noqa: E402


def _split_argv(argv):
    """
    Split the command line into (labels, flags).

    Anything beginning with '-' is a flag (--failfast, -v, etc.).
    Anything else is a test label. This is a simplification of
    Django's own management-command argument parser, and it is
    sufficient for the flags this runner actually forwards.
    """
    labels, flags = [], []
    for arg in argv:
        if arg.startswith('-'):
            flags.append(arg)
        else:
            labels.append(arg)
    return labels, flags


def main():
    django.setup()

    runner_cls = get_runner(settings)
    runner = runner_cls(verbosity=2, interactive=False)

    labels, flags = _split_argv(sys.argv[1:])
    if not labels:
        labels = ['tests']

    # Only --failfast is forwarded as a runner kwarg. Everything
    # else in `flags` is ignored rather than silently mis-parsed —
    # an unknown flag is a caller mistake, and Django's management
    # commands would have errored on it. If you need a different
    # flag, add an explicit branch below.
    runner_kwargs = {}
    if '--failfast' in flags or '-f' in flags:
        runner_kwargs['failfast'] = True

    failures = runner.run_tests(labels, **runner_kwargs)
    sys.exit(bool(failures))


if __name__ == '__main__':
    main()
