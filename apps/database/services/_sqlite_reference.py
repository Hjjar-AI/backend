# backend/apps/database/services/_sqlite_reference.py
"""
Reference SQLite implementation of BackupService.

NOT IMPORTED BY ANYTHING. This module preserves the SQLite branches
that used to sit commented out inside each BackupService method. It
was extracted so the active module (`backup_service.py`) no longer
carries ~200 lines of dead-comment review surface.

The code below is runnable. Every function is a faithful port of the
commented block it replaces.

HOW TO RE-ENABLE SQLITE
-----------------------
1. In config/settings.py, switch DATABASES['default'] to the SQLite
   backend (there is a commented block near the top of the DATABASES
   section).

2. In `backup_service.py`, replace the SQLite "unsupported backend"
   fall-through in each method with a dispatch:

       if _is_sqlite():
           from ._sqlite_reference import get_info as _sqlite_get_info
           return _sqlite_get_info()
       if _is_mariadb():
           ... existing MariaDB branch ...

   Do this once per method (get_info, create_backup, list_backups,
   restore_backup, clear_database).

3. In `backup_service.clear_database`, replace the
   `if _is_mariadb():` AUTO_INCREMENT block with a matching dispatch
   that calls `_sqlite_reference.reset_autoincrement(cursor, model_names)`.

No other file needs to change — the public surface
(`BackupService.X(...)`) is identical for both backends.
"""

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import connections
from django.utils import timezone


logger = logging.getLogger(__name__)


def _sqlite_db_path():
    return settings.DATABASES['default']['NAME']


def get_info():
    """SQLite port of BackupService.get_info()."""
    db_path = _sqlite_db_path()
    if not os.path.exists(db_path):
        return {'error': 'قاعدة البيانات غير موجودة', 'code': 404}

    size = os.path.getsize(db_path)
    modified = datetime.fromtimestamp(
        os.path.getmtime(db_path)
    ).strftime('%Y-%m-%d %H:%M:%S')

    from apps.questions.models import Question, Category
    from apps.users.models import User
    from apps.exams.models import TestHistory

    return {
        'file_path': os.path.abspath(db_path),
        'file_size': f'{size / 1024:.2f} KB',
        'file_modified': modified,
        'database_type': 'SQLite',
        'total_questions':  Question.objects.count(),
        'verified_count':   Question.objects.filter(verified=True).count(),
        'total_users':      User.objects.count(),
        'total_categories': Category.objects.count(),
        'total_sessions':   TestHistory.objects.count(),
    }


def create_backup():
    """SQLite port of BackupService.create_backup()."""
    db_path = _sqlite_db_path()
    if not os.path.exists(db_path):
        return {'error': 'قاعدة البيانات غير موجودة', 'code': 404}

    backup_dir = Path(settings.BACKUP_FOLDER)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
    backup_path = backup_dir / f'questions_backup_{timestamp}.db'

    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(str(backup_path))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        logger.info('Backup created: %s', backup_path)
    except Exception as e:
        logger.exception('Failed to create backup: %s', e)
        if backup_path.exists():
            backup_path.unlink()
        return {'error': 'فشل إنشاء النسخة الاحتياطية', 'code': 500}

    return {'message': 'تم إنشاء نسخة احتياطية', 'path': str(backup_path)}


def list_backups():
    """
    SQLite port of BackupService.list_backups().

    The only difference from the MariaDB implementation is the glob
    pattern: `*.db` instead of `*.sql`.
    """
    backup_dir = Path(settings.BACKUP_FOLDER)
    if not backup_dir.exists():
        return {'items': [], 'total': 0}

    backups = []
    for f in backup_dir.glob('*.db'):
        size = f.stat().st_size
        modified = datetime.fromtimestamp(f.stat().st_mtime)
        backups.append({
            'name':     f.name,
            'size':     f'{size / 1024:.2f} KB',
            'modified': modified.strftime('%Y-%m-%d %H:%M:%S'),
        })
    backups.sort(key=lambda x: x['modified'], reverse=True)
    return {'items': backups, 'total': len(backups)}


def restore_backup(backup_name):
    """SQLite port of BackupService.restore_backup()."""
    backup_dir = Path(settings.BACKUP_FOLDER)
    target_path = (backup_dir / backup_name).resolve()

    # Path traversal guard — must stay in sync with the MariaDB
    # branch in backup_service.py.
    try:
        target_path.relative_to(backup_dir.resolve())
    except ValueError:
        return {'error': 'مسار ملف غير مسموح به', 'code': 400}

    if not target_path.exists():
        return {'error': 'الملف غير موجود', 'code': 404}

    # Integrity check on the candidate file BEFORE we touch the live
    # database. A corrupt backup must not overwrite a working db.
    try:
        conn = sqlite3.connect(str(target_path))
        cursor = conn.cursor()
        cursor.execute('PRAGMA integrity_check')
        result = cursor.fetchone()
        conn.close()
        if result[0] != 'ok':
            return {'error': 'النسخة الاحتياطية تالفة', 'code': 400}
    except sqlite3.Error as e:
        logger.exception('Integrity check failed: %s', e)
        return {'error': 'النسخة الاحتياطية تالفة', 'code': 400}

    # Pre-restore safety snapshot. If this fails, do NOT proceed.
    db_path = _sqlite_db_path()
    timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
    pre_backup = backup_dir / f'pre_restore_{timestamp}.db'

    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(str(pre_backup))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    except Exception as e:
        logger.exception('Failed to create pre-restore backup: %s', e)
        return {'error': 'فشل إنشاء نسخة الأمان قبل الاستعادة', 'code': 500}

    # Close Django's pooled connection before replacing the file.
    # On Linux/macOS, unlinking a file that is still open merely
    # drops its directory entry; the inode survives until the last
    # fd closes. Without this, the next command in this process
    # would keep reading the OLD inode.
    connections.close_all()

    try:
        src = sqlite3.connect(str(target_path))
        dst = sqlite3.connect(db_path)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    except Exception as e:
        logger.exception('Failed to restore backup: %s', e)
        return {'error': 'فشل استعادة النسخة الاحتياطية', 'code': 500}

    return {'message': 'تم استعادة قاعدة البيانات بنجاح'}


def reset_autoincrement(cursor, model_names):
    """
    SQLite-specific AUTO_INCREMENT reset, called after the shared
    model-deletion loop in `clear_database`.

    SQLite stores sequence state in the `sqlite_sequence` table;
    deleting a row does not remove its sequence entry. The MariaDB
    branch uses ALTER TABLE ... AUTO_INCREMENT = 1 instead.

    `cursor`       — an open Django DB cursor.
    `model_names`  — iterable of 'app.Model' strings already deleted.

    NOTE: `django.db.connection` (singular) is a lazy proxy that
    resolves to a backend only after Django's app registry has been
    populated. It is imported inside this function so that a
    future caller who imports this module early (before
    `django.setup()`) does not trigger a `AppRegistryNotReady`
    error at module load.
    """
    from django.apps import apps
    from django.db import connection

    for model_name in model_names:
        model = apps.get_model(model_name)
        table_name = model._meta.db_table
        cursor.execute(
            "DELETE FROM sqlite_sequence WHERE name = ?",
            [table_name],
        )

    # Sweep any remaining sequence entries whose table is now empty
    # (a table not listed in model_names but still holding a stale
    # sequence row).
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    tables = [row[0] for row in cursor.fetchall()]
    for table in tables:
        quoted_table = connection.ops.quote_name(table)
        cursor.execute(f"SELECT COUNT(*) FROM {quoted_table}")
        count = cursor.fetchone()[0]
        if count == 0:
            cursor.execute(
                "DELETE FROM sqlite_sequence WHERE name = ?",
                [table],
            )