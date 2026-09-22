# backend/apps/core/management/_app_registry.py
"""
App registry shared by the `bootstrap` management command and the
standalone SQLite launcher (`backend/start_sqlite.py`).

WHY THIS MODULE EXISTS
----------------------
There are two independent "first run" pipelines in this codebase:

  • `manage.py bootstrap` — the MariaDB-oriented setup command. It
    provisions the database, generates missing migrations, applies
    migrations, and seeds default data.

  • `backend/start_sqlite.py` — the standalone SQLite launcher. It
    creates package files, generates missing migrations, applies
    migrations, and seeds default data.

They are intentionally different scripts — bootstrap does
database-level provisioning the SQLite launcher has no use for, and
the SQLite launcher bootstraps itself from outside Django's
management-command system. But they share two facts that MUST stay
in sync:

  • `CUSTOM_APPS` — every app that needs a migrations package for
    Django's migration loader to find its migrations. If an app is
    added to `INSTALLED_APPS` but not to this list, `manage.py
    migrate` silently skips its migrations and its tables never get
    created.

  • `MODEL_APPS` — the subset of `CUSTOM_APPS` that actually defines
    models. Only these need `makemigrations` to generate an
    `0001_initial.py`, and only these are wiped by `bootstrap
    --clean`.

Both lists used to be duplicated verbatim in `bootstrap.py` and
`start_sqlite.py`. A new app added to one file but not the other
broke silently — the launcher that did not know about the app left
the app's tables unmigrated, and the failure only surfaced the first
time someone queried those tables on a fresh install.

Extracting the lists here makes "update one place" structural rather
than a convention.

SEED-STEP LOGIC IS DELIBERATELY NOT SHARED
------------------------------------------
The two launchers seed different data sets on first run:
`start_sqlite.py` runs `seed --only data --only capabilities`
(explicitly omitting `tips`, and saying so in its console output),
while `bootstrap.py` runs `seed` with no `--only`, which falls
through to `seed.py`'s `_DEFAULT_SEEDERS = ('data', 'tips',
'capabilities')`.

That difference is intentional — a local SQLite dev instance does
not want onboarding tips injected, a real deployment does — and it
is documented in the seed command's own `_DEFAULT_SEEDERS` comment.
Only the app registry is shared; the seed selection stays local to
each launcher.

IMPORT SAFETY
-------------
This module contains only tuples and the `APP_INIT_CONTENT` string.
It imports nothing from Django, and it does not require
`django.setup()` to have run. That is deliberate: `start_sqlite.py`
imports it BEFORE calling `django.setup()`, and a top-level Django
import here would raise `AppRegistryNotReady`.
"""

CUSTOM_APPS = (
    'core',
    'users',
    'questions',
    'learning',
    'feedback',
    'exams',
    'master_exams',
    'groups',
    'planning',
    'analytics',
    'database',
)

# Subset of CUSTOM_APPS that define real models. Only these need
# `makemigrations` to generate `0001_initial.py`, and only these are
# wiped by `bootstrap --clean`. `analytics` and `database` are pure
# service layers (see their models.py files — both are comment-only
# stubs).
MODEL_APPS = (
    'core',
    'users',
    'questions',
    'learning',
    'feedback',
    'exams',
    'master_exams',
    'groups',
    'planning',
)

# Content written to `backend/apps/__init__.py` when missing.
#
# The file makes `apps` a regular Python package rather than a
# PEP 420 namespace package. Without it, Django's migration loader
# does not find the migration modules of the apps nested underneath
# (`apps.users.migrations`, `apps.core.migrations`, ...), and
# `manage.py migrate` produces a plan that contains only Django's
# four built-in apps — leaving every custom table missing.
#
# Only `start_sqlite.py` writes this file (bootstrap runs inside
# Django, where the package is already present because Django
# imported it to find this module). The string is defined here so
# both callers see the same content if that ever changes.
APP_INIT_CONTENT = """\
# backend/apps/__init__.py
#
# This file makes `apps` a regular Python package rather than a
# PEP 420 namespace package. Without it, Django's migration loader
# does not find the migration modules of the apps nested underneath
# (`apps.users.migrations`, `apps.core.migrations`, ...), and
# `manage.py migrate` produces a plan that contains only Django's
# four built-in apps — leaving every custom table missing.
#
# Keep this file. Do not delete it even though it is empty.
"""