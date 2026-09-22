# backend/apps/database/services/backend.py
"""
Backend detection and MySQL/MariaDB client configuration.

Pure helpers — no I/O beyond writing the temporary credentials file
in `_write_defaults_file`. Kept in their own module so both
`backup_service.py` and any future backend-specific module can
import them without pulling in the full service class.
"""

import os
import tempfile

from django.conf import settings
from django.db import connection


def _is_sqlite():
    return connection.vendor == 'sqlite'


def _is_mariadb():
    # `connection.vendor` returns 'mysql' for both MySQL and MariaDB.
    return connection.vendor == 'mysql'


def _mysql_config():
    db = settings.DATABASES['default']
    return {
        'name':     db['NAME'],
        'user':     db['USER'],
        'password': db['PASSWORD'],
        'host':     db.get('HOST') or '127.0.0.1',
        'port':     str(db.get('PORT') or '3306'),
    }


def _write_defaults_file():
    """
    Write a temporary MySQL client credentials file (mode 0600).

    Passing --password on the mysqldump/mysql command line exposes the
    password to every user on the host via `ps`. The
    --defaults-extra-file mechanism is the client's own answer to this
    problem.
    """
    cfg = _mysql_config()
    fd, path = tempfile.mkstemp(prefix='quiz_my_', suffix='.cnf')
    with os.fdopen(fd, 'w') as f:
        f.write('[client]\n')
        f.write(f"user={cfg['user']}\n")
        f.write(f"password={cfg['password']}\n")
        f.write(f"host={cfg['host']}\n")
        f.write(f"port={cfg['port']}\n")
    os.chmod(path, 0o600)
    return path