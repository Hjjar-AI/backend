# backend/apps/database/services/backup_service.py
"""
Backup / restore / clear operations against the configured database.

The live implementation targets MariaDB / MySQL. The previous SQLite
implementation is preserved as a runnable reference module
(`._sqlite_reference`) — it is not imported here. Every method
below returns a clean "backend not supported" error when the
configured database is neither MariaDB nor MySQL.

CALLERS
-------
`apps/database/views.py` imports `BackupService` through the
package `__init__.py`. Every public method below keeps its original
name, signature, and return shape.

UNSUPPORTED-BACKEND ERROR (fix — four hand-written copies)
----------------------------------------------------------
The dict `{'error': 'نوع قاعدة البيانات غير مدعوم', 'code': 500}`
was written out literally in all four methods that dispatch on the
backend (get_info, create_backup, list_backups, restore_backup).
A future wording tweak or status-code change had to be made in
four places or the API would present inconsistent errors for the
same condition. `_unsupported_backend()` is now the single source
and returns a fresh dict each call so a caller cannot mutate a
shared instance into the wrong state.
"""

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import connection, connections
from django.utils import timezone
from apps.core.artifacts import reserve_artifact_path

from .backend import _is_mariadb, _mysql_config, _write_defaults_file


logger = logging.getLogger(__name__)


_UNSUPPORTED_BACKEND_MESSAGE = 'نوع قاعدة البيانات غير مدعوم'


def _unsupported_backend():
    """Return a fresh error dict for the unsupported-backend case."""
    return {'error': _UNSUPPORTED_BACKEND_MESSAGE, 'code': 500}


class BackupService:

    # ── Info ──────────────────────────────────────────────────────────

    @staticmethod
    def get_info():
        if _is_mariadb():
            cfg = _mysql_config()
            try:
                with connection.cursor() as cur:
                    cur.execute(
                        "SELECT COALESCE(SUM(data_length + index_length), 0) "
                        "FROM information_schema.TABLES "
                        "WHERE table_schema = %s",
                        [cfg['name']],
                    )
                    size_bytes = cur.fetchone()[0] or 0
            except Exception:
                logger.exception('Failed to read database size')
                size_bytes = 0

            from apps.questions.models import Question, Category
            from apps.users.models import User
            from apps.exams.models import TestHistory

            return {
                'file_path':     f"mysql://{cfg['host']}:{cfg['port']}/{cfg['name']}",
                'file_size':     f'{size_bytes / 1024:.2f} KB',
                'file_modified': timezone.now().strftime('%Y-%m-%d %H:%M:%S'),
                'database_type': 'MariaDB',
                'total_questions':  Question.objects.count(),
                'verified_count':   Question.objects.filter(verified=True).count(),
                'total_users':      User.objects.count(),
                'total_categories': Category.objects.count(),
                'total_sessions':   TestHistory.objects.count(),
            }

        return _unsupported_backend()

    # ── Create ────────────────────────────────────────────────────────

    @staticmethod
    def create_backup(safety=False):
        if _is_mariadb():
            cfg = _mysql_config()
            backup_dir = Path(settings.BACKUP_FOLDER)
            backup_dir.mkdir(parents=True, exist_ok=True)
            prefix = 'pre_restore' if safety else 'questions_backup'
            backup_path = reserve_artifact_path(backup_dir, prefix, '.sql')

            # Close Django's pooled connections before dumping so a
            # half-open transaction cannot hold a metadata lock that
            # blocks --single-transaction.
            connections.close_all()

            cnf = _write_defaults_file()
            try:
                with open(backup_path, 'w') as out:
                    result = subprocess.run(
                        [
                            'mysqldump',
                            f'--defaults-extra-file={cnf}',
                            '--single-transaction',
                            '--routines',
                            '--triggers',
                            '--events',
                            cfg['name'],
                        ],
                        stdout=out,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
            except FileNotFoundError:
                logger.exception('mysqldump not found on PATH')
                backup_path.unlink(missing_ok=True)
                return {'error': 'أداة النسخ الاحتياطي غير مثبتة على الخادم', 'code': 500}
            finally:
                os.unlink(cnf)

            if result.returncode != 0:
                logger.error('mysqldump failed (rc=%s): %s',
                             result.returncode, result.stderr)
                backup_path.unlink(missing_ok=True)
                return {'error': 'فشل إنشاء النسخة الاحتياطية', 'code': 500}

            logger.info('Backup created: %s', backup_path)
            return {'message': 'تم إنشاء نسخة احتياطية', 'path': str(backup_path)}

        return _unsupported_backend()

    # ── List ──────────────────────────────────────────────────────────

    @staticmethod
    def list_backups():
        """
        List backup files on disk.

        FIXED BEHAVIOUR: the previous version silently globbed `*.db`
        when the backend was not MariaDB — meaning on SQLite it
        returned a list of `.db` files that the (also inactive)
        SQLite restore path could not consume, and on any third
        backend (Postgres, for example) it returned an empty list
        rather than an error. It now returns a clean
        "backend not supported" error on anything other than MariaDB,
        matching the other four methods in this class.
        """
        if not _is_mariadb():
            return _unsupported_backend()

        backup_dir = Path(settings.BACKUP_FOLDER)
        if not backup_dir.exists():
            return {'items': [], 'total': 0}

        backups = []
        for f in backup_dir.glob('*.sql'):
            size = f.stat().st_size
            modified = datetime.fromtimestamp(f.stat().st_mtime)
            backups.append({
                'name':     f.name,
                'size':     f'{size / 1024:.2f} KB',
                'modified': modified.strftime('%Y-%m-%d %H:%M:%S'),
            })
        backups.sort(key=lambda x: x['modified'], reverse=True)
        return {'items': backups, 'total': len(backups)}

    # ── Restore ───────────────────────────────────────────────────────

    @staticmethod
    def restore_backup(backup_name):
        backup_dir = Path(settings.BACKUP_FOLDER)
        target_path = (backup_dir / backup_name).resolve()

        # Path traversal guard — same in both backends, keep as-is.
        try:
            target_path.relative_to(backup_dir.resolve())
        except ValueError:
            return {'error': 'مسار ملف غير مسموح به', 'code': 400}

        if not target_path.exists():
            return {'error': 'الملف غير موجود', 'code': 404}

        if _is_mariadb():
            cfg = _mysql_config()

            # Pre-restore safety backup (a full mysqldump of the
            # current state). If this fails, do NOT proceed.
            safety = BackupService.create_backup(safety=True)
            if 'error' in safety:
                logger.error('Aborting restore: safety backup failed')
                return safety

            connections.close_all()

            # The dump emitted by mysqldump contains
            #   SET FOREIGN_KEY_CHECKS=0
            #   DROP TABLE IF EXISTS ...
            #   CREATE TABLE ...
            # so piping it over the live database is sufficient — no
            # DROP DATABASE / CREATE DATABASE needed, and therefore no
            # extra GRANT privileges. If your deployment prefers the
            # drop-and-recreate approach, replace this block.
            cnf = _write_defaults_file()
            try:
                with open(target_path, 'r', encoding='utf-8') as src:
                    result = subprocess.run(
                        ['mysql', f'--defaults-extra-file={cnf}', cfg['name']],
                        stdin=src,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
            except FileNotFoundError:
                logger.exception('mysql client not found on PATH')
                return {'error': 'أداة الاستعادة غير مثبتة على الخادم', 'code': 500}
            finally:
                os.unlink(cnf)

            if result.returncode != 0:
                logger.error('mysql restore failed (rc=%s): %s',
                             result.returncode, result.stderr)
                return {'error': 'فشل استعادة النسخة الاحتياطية', 'code': 500}

            # Force every worker to re-open against the restored schema.
            connections.close_all()
            logger.info('Restore completed from %s', backup_name)
            return {'message': 'تم استعادة قاعدة البيانات بنجاح'}

        return _unsupported_backend()

    # ── Clear (wipe content, keep schema) ─────────────────────────────

    @staticmethod
    def clear_database():
        backup_result = BackupService.create_backup()
        if 'error' in backup_result:
            return backup_result

        from django.apps import apps
        from django.db import transaction

        # Order matters. MasterExamQuestion precedes both Question
        # (FK PROTECT from question side) and MasterExam, and its
        # through-relation must be cleared before the draft questions
        # it references are deleted.
        #
        # This list is backend-agnostic. It no longer contains
        # 'exams.StudySession' or 'exams.StudyProgress' — StudySession
        # was consolidated into TestHistory, and StudyProgress was
        # dead code removed in the same release.
        models_to_clear = [
            'master_exams.MasterExamQuestion',
            'master_exams.MasterExamAttempt',
            'master_exams.MasterExamAcknowledgement',
            'master_exams.MasterExam',
            'learning.UserQuestionAttempt',
            'feedback.Bookmark',
            'feedback.QuestionFlag',
            'feedback.QuestionRating',
            'questions.QuestionTag',
            'questions.Question',
            'questions.ClinicalCase',
            'questions.Category',
            'questions.Tag',
            'exams.Blueprint',
            'exams.ExamSession',
            'exams.TestHistory',
            'planning.StudyPlanner',
        ]

        with transaction.atomic():
            for model_name in models_to_clear:
                model = apps.get_model(model_name)
                model.objects.all().delete()

            # MariaDB: reset AUTO_INCREMENT on the wiped tables.
            # SQLite: call _sqlite_reference.reset_autoincrement —
            # see that module's docstring for the re-enable recipe.
            if _is_mariadb():
                cursor = connection.cursor()
                for model_name in models_to_clear:
                    model = apps.get_model(model_name)
                    table_name = model._meta.db_table
                    cursor.execute(
                        f"ALTER TABLE `{table_name}` AUTO_INCREMENT = 1"
                    )

        return {'message': 'تم مسح قاعدة البيانات'}