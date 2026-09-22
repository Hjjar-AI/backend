# backend/apps/core/management/commands/doctor.py
"""
Read-only health check for a deployed or in-development Mukhtabir
instance.

Usage:
    python manage.py doctor

WHAT IT CHECKS
--------------
  • Django configuration (DEBUG, SECRET_KEY, ALLOWED_HOSTS)
  • Database connectivity and migration state
  • Cache backend round-trip
  • WeasyPrint import (PDF export availability)
  • Arabic TTF reachability (PDF typography)
  • Writable directories (uploads, exports, backups, media)
  • Frontend build presence (for the served SPA)
  • Whether a superuser exists

Every check reports one of:
  ✓ OK     the check passed
  ! WARN   the check failed but the app still runs (degraded)
  ✗ FAIL   the check failed and something will break

Exit code is 0 when no FAIL, 1 when at least one FAIL. This lets
`doctor` be used as a CI gate or a `systemd` pre-flight check.
"""

import os
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader


from apps.core.fonts import arabic_font_candidates, resolve_arabic_font_path


class Command(BaseCommand):
    help = (
        'Run a read-only health check on the current deployment. '
        'Never writes to the database or the filesystem.'
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fails = 0
        self.warns = 0

    # ── Entry point ─────────────────────────────────────────────────

    def handle(self, *args, **options):
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            '═══════════════════════════════════════════════════════════'
        ))
        self.stdout.write(self.style.MIGRATE_HEADING(
            '  Mukhtabir doctor'
        ))
        self.stdout.write(self.style.MIGRATE_HEADING(
            '═══════════════════════════════════════════════════════════'
        ))
        self.stdout.write('')

        self._section('Configuration')
        self._check_debug()
        self._check_secret_key()
        self._check_allowed_hosts()
        self._check_https()

        self._section('Database')
        self._check_db_connectivity()
        self._check_migrations_applied()
        self._check_superuser_exists()

        self._section('Cache')
        self._check_cache_round_trip()

        self._section('PDF export')
        self._check_weasyprint()
        self._check_arabic_font()

        self._section('Filesystem')
        self._check_writable_dir('UPLOAD_FOLDER', settings.UPLOAD_FOLDER)
        self._check_writable_dir('EXPORT_FOLDER', settings.EXPORT_FOLDER)
        self._check_writable_dir('BACKUP_FOLDER', settings.BACKUP_FOLDER)
        self._check_writable_dir('MEDIA_ROOT', settings.MEDIA_ROOT)

        self._section('Frontend')
        self._check_frontend_build()

        # ── Summary ──────────────────────────────────────────────
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            '─────────────────────────────────────────────────────────'
        ))
        if self.fails == 0 and self.warns == 0:
            self.stdout.write(self.style.SUCCESS(
                f'  ✓ All checks passed.'
            ))
        else:
            parts = []
            if self.fails:
                parts.append(self.style.ERROR(f'{self.fails} FAIL'))
            if self.warns:
                parts.append(self.style.WARNING(f'{self.warns} WARN'))
            self.stdout.write(
                '  ' + ' · '.join(parts)
            )
        self.stdout.write(self.style.MIGRATE_HEADING(
            '─────────────────────────────────────────────────────────'
        ))
        self.stdout.write('')

        if self.fails:
            raise SystemExit(1)

    # ── Reporting helpers ───────────────────────────────────────────

    def _section(self, title):
        self.stdout.write(self.style.MIGRATE_LABEL(f'▸ {title}'))

    def _ok(self, label, detail=''):
        suffix = f'  ({detail})' if detail else ''
        self.stdout.write(f'    {self.style.SUCCESS("✓")} {label}{suffix}')

    def _warn(self, label, detail=''):
        self.warns += 1
        suffix = f'  ({detail})' if detail else ''
        self.stdout.write(f'    {self.style.WARNING("!")} {label}{suffix}')

    def _fail(self, label, detail=''):
        self.fails += 1
        suffix = f'  ({detail})' if detail else ''
        self.stdout.write(f'    {self.style.ERROR("✗")} {label}{suffix}')

    # ── Configuration ───────────────────────────────────────────────

    def _check_debug(self):
        if settings.DEBUG:
            self._warn(
                'DEBUG is True',
                'intended for development; set DEBUG=False in production',
            )
        else:
            self._ok('DEBUG is False')

    def _check_secret_key(self):
        key = getattr(settings, 'SECRET_KEY', '') or ''
        if not key:
            self._fail('SECRET_KEY is empty')
            return
        if len(key) < 32:
            self._warn(
                'SECRET_KEY is shorter than 32 characters',
                f'length={len(key)}',
            )
            return
        if key.startswith('django-insecure'):
            self._fail(
                'SECRET_KEY uses the Django development prefix',
                'generate a real secret before deploying',
            )
            return
        self._ok('SECRET_KEY looks well-formed')

    def _check_allowed_hosts(self):
        hosts = getattr(settings, 'ALLOWED_HOSTS', []) or []
        if not hosts:
            self._fail('ALLOWED_HOSTS is empty')
            return
        if '*' in hosts and not settings.DEBUG:
            self._fail(
                'ALLOWED_HOSTS contains "*" while DEBUG=False',
                'set an explicit list in backend/.env',
            )
            return
        self._ok('ALLOWED_HOSTS is configured', ', '.join(hosts))

    def _check_https(self):
        secure = getattr(settings, 'SESSION_COOKIE_SECURE', False)
        if secure:
            self._ok('SESSION_COOKIE_SECURE is True')
        else:
            self._warn(
                'SESSION_COOKIE_SECURE is False',
                'fine on localhost; enables cleartext session cookies over HTTP',
            )

    # ── Database ────────────────────────────────────────────────────

    def _check_db_connectivity(self):
        try:
            with connection.cursor() as cur:
                cur.execute('SELECT 1')
                cur.fetchone()
        except Exception as exc:
            self._fail(
                f'Database connection failed: {type(exc).__name__}',
                str(exc)[:120],
            )
            return
        self._ok(
            'Database connection OK',
            f'{connection.vendor}',
        )

    def _check_migrations_applied(self):
        try:
            loader = MigrationLoader(connection)
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(loader.graph.leaf_nodes())
        except Exception as exc:
            self._fail(
                f'Could not inspect migration state: {type(exc).__name__}',
                str(exc)[:120],
            )
            return

        if not plan:
            self._ok('All migrations are applied')
            return

        self._fail(
            f'{len(plan)} unapplied migration(s)',
            'run `python manage.py migrate`',
        )

    def _check_superuser_exists(self):
        try:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            if not User.objects.filter(is_superuser=True, is_active=True).exists():
                self._fail(
                    'No active superuser found',
                    'run `python manage.py seed_data` or createsuperuser',
                )
                return
        except Exception as exc:
            self._fail(
                f'Superuser check failed: {type(exc).__name__}',
                str(exc)[:120],
            )
            return
        self._ok('At least one active superuser exists')

    # ── Cache ───────────────────────────────────────────────────────

    def _check_cache_round_trip(self):
        key = '_doctor_probe'
        value = 'ok'
        try:
            cache.set(key, value, 5)
            read = cache.get(key)
            cache.delete(key)
        except Exception as exc:
            self._fail(
                f'Cache write/read failed: {type(exc).__name__}',
                str(exc)[:120],
            )
            return
        if read != value:
            self._fail(
                'Cache round-trip returned the wrong value',
                f'read back {read!r}',
            )
            return

        backend = settings.CACHES['default']['BACKEND']
        short = backend.rsplit('.', 1)[-1]
        if short == 'LocMemCache' and not settings.DEBUG:
            self._warn(
                'LocMemCache is in use while DEBUG=False',
                'throttle counters and login buckets will not be shared '
                'across workers',
            )
            return
        self._ok('Cache round-trip OK', short)

    # ── PDF export ──────────────────────────────────────────────────

    def _check_weasyprint(self):
        try:
            import weasyprint  # noqa: F401
            version = getattr(weasyprint, '__version__', 'unknown')
        except ImportError as exc:
            self._warn(
                'WeasyPrint is not installed — PDF export will return 500',
                str(exc)[:120],
            )
            return
        except Exception as exc:
            # WeasyPrint raises OSError at import if a system library
            # (libpango, libcairo) is missing.
            self._fail(
                f'WeasyPrint import failed: {type(exc).__name__}',
                'install libpango-1.0-0, libcairo2, libharfbuzz0b '
                '(see reqs_n_start.txt section 0)',
            )
            return
        self._ok('WeasyPrint is importable', f'v{version}')

    def _check_arabic_font(self):
        font_path = resolve_arabic_font_path()
        if font_path is not None:
            self._ok('Arabic TTF is reachable', str(font_path))
            return
        self._warn(
            'Arabic TTF not found — PDF export will fall back to a '
            'system font',
            f'looked in {len(arabic_font_candidates())} location(s)',
        )

    # ── Filesystem ──────────────────────────────────────────────────

    def _check_writable_dir(self, label, path):
        p = Path(path)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._fail(
                f'{label} is not creatable: {type(exc).__name__}',
                str(p),
            )
            return
        try:
            with tempfile.NamedTemporaryFile(dir=p, delete=True):
                pass
        except Exception as exc:
            self._fail(
                f'{label} is not writable: {type(exc).__name__}',
                str(p),
            )
            return
        self._ok(f'{label} is writable', str(p))

    # ── Frontend ────────────────────────────────────────────────────

    def _check_frontend_build(self):
        index = Path(settings.FRONTEND_DIST) / 'index.html'
        if index.is_file():
            size = index.stat().st_size
            self._ok('Frontend build present', f'{size} bytes')
            return
        self._warn(
            'Frontend build not found — the SPA will return a 503 placeholder',
            f'run `pnpm build` in {settings.FRONTEND_DIR}',
        )
