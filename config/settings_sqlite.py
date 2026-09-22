"""Local SQLite settings used by :mod:`start_sqlite`.

The regular settings module remains the MariaDB / Memcached (or Redis)
configuration used by the normal launcher. This overlay is
intentionally single-process: SQLite and LocMemCache are convenient
for a self-contained local instance, but neither is intended for a
multi-worker deployment.

RUNNING ALONGSIDE A MARIADB INSTANCE
------------------------------------
Both this configuration and the regular one can run at the same
time, in two terminals, from the same ``backend/`` checkout. The
parts that differ are isolated by this file:

  • The database — SQLite file vs. MariaDB server.

  • The cache — in-process LocMemCache vs. a shared Memcached/Redis.

  • The session and CSRF cookie names. Browsers scope cookies by
    host, NOT by port, so two servers on ``localhost:5004`` and
    ``localhost:5005`` would otherwise overwrite each other's
    ``sessionid`` and log the user out on every tab switch. The
    rename below gives the SQLite instance its own cookies.

  • The four on-disk data folders — media, uploads, exports, and
    backups — AND the SQLite database file itself. Every one of
    them is redirected into ``BASE_DIR/SQLite/`` so nothing the
    SQLite instance writes ever appears in, or overwrites a file
    from, the MariaDB instance running from the same tree.

The parts that are NOT isolated and are shared by design:

  • Migration files under ``apps/*/migrations/``. Both instances
    read the same migrations, because both instances run the same
    models. Migrations are source code, not per-database state —
    they describe the schema that the model classes imply, and the
    model classes are identical for both instances.

  • ``.env`` — ``DJANGO_SECRET_KEY``, ``DEBUG``, ``ALLOWED_HOSTS``,
    and the other environment-driven settings.

  • ``frontend/dist/`` — the built SPA.

FOLDER LAYOUT
-------------
::

    backend/
    ├── SQLite/
    │   ├── db.sqlite3    ← the SQLite database itself
    │   ├── media/        ← SQLite instance question images
    │   ├── uploads/      ← SQLite instance import staging
    │   ├── exports/      ← SQLite instance generated exports
    │   └── backups/      ← SQLite instance database backups
    ├── media/            ← MariaDB instance question images
    ├── uploads/
    ├── exports/
    └── backups/

The whole ``backend/SQLite/`` subtree is disposable for the four
folder overrides — delete the folders at any time and they are
recreated on the next launch. The database file is NOT disposable in
that sense: deleting ``SQLite/db.sqlite3`` wipes the SQLite
instance's data, exactly as deleting any database file would. The
launcher recreates it with a fresh schema on the next start, but the
rows are gone.

``SQLITE_DB_PATH`` overrides the default database location. An
absolute path is used as-is; a relative path is resolved against
``BASE_DIR``, not against the current working directory, so the
launcher behaves the same no matter where it is invoked from.
"""

import os
from pathlib import Path


# Local launches must be able to use Django's development server even
# when backend/.env contains production values. Set this before
# importing the base settings because that module validates
# SECRET_KEY and ALLOWED_HOSTS while it is imported.
os.environ['DEBUG'] = 'True'

from .settings import *  # noqa: E402,F401,F403


DEBUG = True


# ── Filesystem isolation root ───────────────────────────────────────
#
# Every SQLite-instance path is derived from this one root. Moving the
# whole instance to a different location is a single-line change here.
_sqlite_root = BASE_DIR / 'SQLite'


# ── Database ────────────────────────────────────────────────────────
#
# The default lives inside ``SQLite/`` rather than at ``BASE_DIR``
# directly, so everything the SQLite instance owns is under one
# directory. ``SQLITE_DB_PATH`` still overrides; a relative value is
# resolved against ``BASE_DIR`` so the launcher's behaviour does not
# depend on the process's current working directory.
_database_path = Path(
    os.environ.get('SQLITE_DB_PATH', str(_sqlite_root / 'db.sqlite3'))
).expanduser()
if not _database_path.is_absolute():
    _database_path = BASE_DIR / _database_path

# The folder is created here rather than in ``start_sqlite.py``
# because ``DATABASES['default']['NAME']`` is read by ``django.setup``
# and by every management command. If the parent folder does not
# exist, SQLite refuses to open the file with "unable to open database
# file". Creating it at settings-import time means ``manage.py``
# invocations — migrate, showmigrations, seed_pro_users — all work
# without going through the launcher first.
_database_path.parent.mkdir(parents=True, exist_ok=True)

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': _database_path,
        'OPTIONS': {
            # Give concurrent requests a short opportunity to finish
            # rather than immediately returning "database is locked".
            'timeout': 20,
        },
    },
}


# ── Cache ───────────────────────────────────────────────────────────
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'quiz-sqlite-local',
    },
}


# ── Cookie isolation ────────────────────────────────────────────────
#
# Distinct names so the SQLite instance never collides with a MariaDB
# instance running on another port of the same host. Cookie scope is
# host + path only; the port is not part of the scope. Two servers
# on ``localhost`` would otherwise share ``sessionid`` and
# ``csrftoken``, and switching tabs would log the user out of one of
# them on every navigation.
#
# The names differ ONLY on the SQLite side. The MariaDB instance
# keeps Django's defaults (``sessionid`` / ``csrftoken``), so
# existing sessions in a browser continue to work against the
# MariaDB server unchanged.
SESSION_COOKIE_NAME = 'sqlite_sessionid'
CSRF_COOKIE_NAME = 'sqlite_csrftoken'


# ── Filesystem isolation ────────────────────────────────────────────
#
# The base settings module points MEDIA_ROOT, UPLOAD_FOLDER,
# EXPORT_FOLDER, and BACKUP_FOLDER at ``BASE_DIR/...``. Redirecting
# them here means a file written by the SQLite instance never lands
# in the MariaDB instance's tree — question images, import staging
# files, generated Excel/CSV/JSON/PDF exports, and database backups
# all go into ``backend/SQLite/`` instead.
#
# The four variables below are read by:
#
#   • ``apps/core/tasks.py::cleanup_temp_files`` — sweeps UPLOAD,
#     EXPORT, and BACKUP on a schedule.
#   • ``apps/core/management/commands/doctor.py`` — checks that all
#     four are writable and reports failures at startup.
#   • ``apps/database/services/backup_service.py`` — writes and
#     lists backup files.
#   • ``apps/questions/services/exporting/*`` — writes export files.
#   • ``config/settings.py``'s ``MEDIA_URL`` / ``MEDIA_ROOT`` pair —
#     question images are served from MEDIA_ROOT in DEBUG mode.
#
# Nothing reads these paths by a hard-coded literal, so the override
# is complete.
MEDIA_ROOT    = _sqlite_root / 'media'
UPLOAD_FOLDER = _sqlite_root / 'uploads'
EXPORT_FOLDER = _sqlite_root / 'exports'
BACKUP_FOLDER = _sqlite_root / 'backups'

for _folder in (MEDIA_ROOT, UPLOAD_FOLDER, EXPORT_FOLDER, BACKUP_FOLDER):
    _folder.mkdir(parents=True, exist_ok=True)