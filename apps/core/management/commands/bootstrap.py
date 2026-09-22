# backend/apps/core/management/commands/bootstrap.py
"""
One-shot first-time setup for the Quiz backend.

Usage:
    python manage.py bootstrap
    python manage.py bootstrap --with-sample-questions
    python manage.py bootstrap --clean --with-sample-questions
    python manage.py bootstrap --no-db-setup
    python manage.py bootstrap --db-admin-user root --db-admin-password 'secret'
    python manage.py bootstrap --collectstatic

PIPELINE
--------
  1. Django system checks (fail fast on a broken config)
  2. MariaDB / MySQL database + user provisioning
     (skipped on SQLite or with --no-db-setup)
  3. --clean: drop every table and delete migration files (destructive)
  4. Ensure each custom app has a migrations package
  5. makemigrations (only if any model app lacks migration files)
  6. migrate
  7. seed (the wrapper that runs seed_data, seed_tips, seed_capabilities,
     and optionally seed_sample_questions)
  8. --collectstatic: collect static files (opt-in)
  9. Verify the Arabic TTF is reachable for PDF export
 10. Print a summary with next steps

DATABASE SUPPORT
----------------
  • MariaDB / MySQL — the current production target.
    Step 2 provisions the schema and user from the values in
    config/settings.py, using an administrative connection that is
    separate from the application's own credentials.

  • SQLite — the previous target. Step 2 is a silent no-op; the
    --clean step deletes the DB file plus its journal/wal/shm
    sidecars. Kept for the fallback path.

The branch is selected at runtime from `connection.vendor`.

ADMINISTRATIVE CONNECTION (MariaDB only)
----------------------------------------
Creating a database and user requires privileges the application
user does not have. Bootstrap therefore opens a SEPARATE raw
`MySQLdb` connection for step 2, using admin credentials sourced
from (in order of precedence):

  1. DB_ADMIN_USER / DB_ADMIN_PASSWORD environment variables
     (or the same keys in backend/.env, loaded by settings.py).
     This is the PREFERRED path: values supplied here never appear
     in the process argument list (`ps`) or in shell history.

  2. --db-admin-user / --db-admin-password command-line options.
     Supported for convenience, but the operator should be aware
     that a password passed on the command line is visible to any
     user on the host via `ps` and is recorded in shell history.

  3. Fallback: user 'root', empty password. This works on a default
     Debian/Ubuntu MariaDB install when the process runs as OS root
     (Unix-socket authentication). On any other setup it will fail
     with a clear message that tells the operator which variables
     to set.

The application's own connection (from .env) is used to VERIFY the
provisioning succeeded. It is never used to CREATE anything.

FLAG-NAME NOTE
--------------
Django automatically adds `--version` and `--skip-checks` to every
management command via `BaseCommand.create_parser()`. Adding either
of those names again in `add_arguments` raises an argparse
`ArgumentError: conflicting option string`. Our custom "skip the
system checks step" flag is therefore named `--no-checks` (with
`dest='skip_checks'` so the code that reads it is unchanged). It
serves a different purpose from Django's `--skip-checks`: the
built-in flag only skips Django's automatic check invocation, not
the explicit `call_command('check', ...)` that this command runs.
"""

import os
import shutil
import sys
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from apps.core.management._app_registry import CUSTOM_APPS, MODEL_APPS


# Name of the Arabic TTF the PDF exporter embeds. Kept in one place
# so bootstrap and pdf_export.py agree about the filename.
from apps.core.fonts import arabic_font_candidates, resolve_arabic_font_path


class Command(BaseCommand):
    help = (
        'First-time setup: provision the database (MariaDB), apply '
        'migrations, and seed default data. Pass --clean to wipe the '
        'database and migration files first.'
    )

    # ── Argument parsing ─────────────────────────────────────────────

    def add_arguments(self, parser):
        parser.add_argument(
            '--clean',
            action='store_true',
            help=(
                'DESTRUCTIVE: drop every table in the configured '
                'database and delete every migration file under the '
                'custom apps before rebuilding. The entire database '
                'is lost. Requires confirmation.'
            ),
        )
        parser.add_argument(
            '--yes', '-y',
            action='store_true',
            help='Skip the interactive confirmation for --clean (for scripts/CI).',
        )
        parser.add_argument(
            '--with-sample-questions',
            action='store_true',
            help='Also run seed_sample_questions after seeding default data.',
        )
        parser.add_argument(
            '--reset-capabilities',
            action='store_true',
            help=(
                'Forward --reset to the seed wrapper so seed_capabilities '
                'overwrites every RoleCapabilities row with the code '
                'defaults. Discards panel edits.'
            ),
        )

        # ── DB provisioning ────────────────────────────────────────
        parser.add_argument(
            '--no-db-setup',
            dest='db_setup',
            action='store_false',
            default=True,
            help=(
                'Skip the MariaDB database/user provisioning step. Use '
                'this when the database and user have already been '
                'created externally, or when running against SQLite.'
            ),
        )
        parser.add_argument(
            '--db-admin-user',
            default=None,
            help=(
                'Administrative MariaDB user. Lower precedence than '
                'DB_ADMIN_USER in backend/.env. Default: root.'
            ),
        )
        parser.add_argument(
            '--db-admin-password',
            default=None,
            help=(
                'Password for --db-admin-user. Lower precedence than '
                'DB_ADMIN_PASSWORD in backend/.env. WARNING: a value '
                'passed on the command line is visible to every user '
                'on the host via `ps` and is recorded in shell '
                'history — prefer the .env variable.'
            ),
        )

        # ── Miscellaneous ─────────────────────────────────────────
        parser.add_argument(
            '--collectstatic',
            action='store_true',
            help='Run collectstatic at the end of the pipeline.',
        )
        parser.add_argument(
            '--no-checks',
            dest='skip_checks',
            action='store_true',
            help=(
                'Skip the explicit Django system-checks step this command '
                'runs. Not recommended. Note: Django reserves the flag '
                'name `--skip-checks` for its own use; `--no-checks` is '
                'the equivalent here and additionally bypasses the '
                'explicit `call_command("check")` this pipeline performs.'
            ),
        )
        parser.add_argument(
            '--skip-seed',
            action='store_true',
            help=(
                'Skip the seed wrapper entirely. Useful for CI runs '
                'that will seed separately.'
            ),
        )

    # ── Entry point ─────────────────────────────────────────────────

    def handle(self, *args, **options):
        # backend/  (bootstrap.py lives at backend/apps/core/management/commands/)
        # parents: [commands, management, core, apps, backend]
        backend_dir = Path(__file__).resolve().parents[4]
        apps_dir = backend_dir / 'apps'

        # ── 1. System checks ──────────────────────────────────────
        if not options['skip_checks']:
            if not self._run_system_checks():
                raise CommandError(
                    'Django system checks failed. Fix the errors printed '
                    'above and re-run. Pass --no-checks to bypass '
                    '(not recommended).'
                )

        # ── 2. Database + user provisioning ───────────────────────
        if options['db_setup']:
            ok = self._setup_database_and_user(options)
            if not ok:
                raise CommandError(
                    'Database setup failed. Follow the instructions '
                    'above, or provision the database manually and '
                    're-run with --no-db-setup.'
                )

        # ── 3. --clean (destructive) ──────────────────────────────
        if options['clean']:
            self._confirm_clean(assume_yes=options['yes'])
            self._clean(apps_dir)

        # ── 4-7. Migrations + seed pipeline ───────────────────────
        self._bootstrap(apps_dir, options)

        # ── 8. Optional collectstatic ─────────────────────────────
        if options['collectstatic']:
            self.stdout.write('[post] collecting static files…')
            try:
                call_command(
                    'collectstatic',
                    interactive=False,
                    verbosity=0,
                )
                self.stdout.write(self.style.SUCCESS('[post] static files collected'))
            except Exception as exc:
                self.stderr.write(self.style.WARNING(
                    f'[post] collectstatic failed: {type(exc).__name__}: {exc}'
                ))
                self.stderr.write(self.style.WARNING(
                    '[post] The app is functional; admin CSS/JS may be '
                    'unstyled. Re-run `python manage.py collectstatic` '
                    'manually after fixing the cause.'
                ))

        # ── 9. Font check ─────────────────────────────────────────
        self._verify_arabic_font()

        # ── 10. Summary ───────────────────────────────────────────
        self._print_summary(options)

    # ── System checks ────────────────────────────────────────────────

    def _run_system_checks(self):
        """
        Run Django's own `check` command and report a boolean.

        `check` exits via SystemExit(1) on failure, so it has to be
        caught explicitly rather than allowed to propagate — otherwise
        the exception escapes `handle()` and Django prints a bare
        traceback instead of a clean error message.
        """
        self.stdout.write('[1/N] running Django system checks…')
        buf = StringIO()
        try:
            call_command('check', stdout=buf, stderr=buf)
        except SystemExit:
            self.stderr.write(self.style.ERROR(buf.getvalue().rstrip()))
            return False

        # `check` sometimes writes warnings even on success. Surface
        # them without failing.
        output = buf.getvalue().strip()
        if output:
            self.stdout.write(output)
        self.stdout.write(self.style.SUCCESS('[1/N] system checks passed'))
        return True

    # ── Database setup ───────────────────────────────────────────────

    def _setup_database_and_user(self, options):
        """
        Provision the MariaDB schema and application user.

        Returns True on success (including "already provisioned"),
        False on failure. A False return is treated as fatal by
        `handle()`.

        On SQLite or any non-MySQL backend this is a no-op that
        returns True. The vendor is read from `connection.vendor`,
        matching the branch used by the --clean helpers.
        """
        from django.db import connection

        if connection.vendor != 'mysql':
            self.stdout.write(
                f'[2/N] database backend is {connection.vendor!r} — '
                f'skipping MariaDB provisioning'
            )
            return True

        db = settings.DATABASES['default']
        db_name = db['NAME']
        db_user = db['USER']
        db_password = db.get('PASSWORD') or ''
        db_host = db.get('HOST') or '127.0.0.1'
        db_port = int(db.get('PORT') or 3306)

        # MariaDB treats the same username at 'localhost' and at
        # '127.0.0.1' as two different identities. Pick the pattern
        # that matches how the app connects.
        grant_host = 'localhost' if db_host in ('', 'localhost') else db_host

        # ── Fast path: the app user already connects ─────────────
        probe, _ = self._try_connection(db_host, db_port, db_user, db_password)
        if probe is not None:
            probe.close()
            self.stdout.write(self.style.SUCCESS(
                f'[2/N] MariaDB: user {db_user!r} already connects to '
                f'{db_host}:{db_port} — provisioning skipped'
            ))
            return True

        # ── Admin credentials ────────────────────────────────────
        #
        # ENV-FIRST PRECEDENCE (fix — CLI flag leakage)
        # ---------------------------------------------------------
        # The previous order was `--db-admin-password` first, then
        # DB_ADMIN_PASSWORD. That is backwards for a secret: a value
        # passed on the command line is visible to every user on the
        # host via `ps` and is written to the invoking shell's history
        # file. The .env file is 0600 and is read by settings.py from
        # a known location, so it is the safer source.
        #
        # The CLI flags are still accepted for one-off/CI use, but the
        # .env value wins if both are present. `add_arguments` above
        # documents this in the flag help text.
        admin_user = (
            os.environ.get('DB_ADMIN_USER')
            or options.get('db_admin_user')
            or 'root'
        )
        admin_password = (
            os.environ.get('DB_ADMIN_PASSWORD')
            or options.get('db_admin_password')
            or ''
        )

        self.stdout.write(
            f'[2/N] provisioning MariaDB `{db_name}` as admin {admin_user!r}…'
        )

        conn, err = self._try_connection(
            db_host, db_port, admin_user, admin_password,
        )

        # Fall back to empty password on the first attempt — that is
        # what Unix-socket auth as OS root looks like to the driver.
        if conn is None and admin_password:
            conn, _ = self._try_connection(db_host, db_port, admin_user, '')

        if conn is None:
            self._print_db_setup_failure(
                admin_user=admin_user,
                db_host=db_host,
                db_port=db_port,
                db_name=db_name,
                db_user=db_user,
                grant_host=grant_host,
                error=err,
            )
            return False

        # ── Run the DDL ──────────────────────────────────────────
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f'CREATE DATABASE IF NOT EXISTS `{db_name}` '
                    f'CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'
                )
                self.stdout.write(
                    f'[2/N]   database `{db_name}` ensured'
                )

                cur.execute(
                    f"CREATE USER IF NOT EXISTS '{db_user}'@'{grant_host}' "
                    f"IDENTIFIED BY %s",
                    [db_password],
                )
                # CREATE USER IF NOT EXISTS does not update the
                # password of an already-existing user. ALTER does.
                # This keeps the effective password in lockstep with
                # backend/.env on every re-run.
                cur.execute(
                    f"ALTER USER '{db_user}'@'{grant_host}' IDENTIFIED BY %s",
                    [db_password],
                )
                self.stdout.write(
                    f"[2/N]   user '{db_user}'@'{grant_host}' ensured"
                )

                cur.execute(
                    f"GRANT ALL PRIVILEGES ON `{db_name}`.* "
                    f"TO '{db_user}'@'{grant_host}'"
                )
                cur.execute('FLUSH PRIVILEGES')
                self.stdout.write(
                    f"[2/N]   granted ALL on `{db_name}`.* "
                    f"to '{db_user}'@'{grant_host}'"
                )
        except Exception as exc:
            self.stderr.write(self.style.ERROR(
                f'[2/N] DDL failed: {type(exc).__name__}: {exc}'
            ))
            conn.close()
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # ── Verify the app user now connects ─────────────────────
        verify, verify_err = self._try_connection(
            db_host, db_port, db_user, db_password,
        )
        if verify is None:
            self.stderr.write(self.style.ERROR(
                f'[2/N] provisioning ran but the app user still cannot '
                f'connect: {verify_err}'
            ))
            return False
        verify.close()

        self.stdout.write(self.style.SUCCESS(
            f'[2/N] MariaDB ready: {db_name} / {db_user}@{grant_host}'
        ))
        return True

    @staticmethod
    def _try_connection(host, port, user, password):
        """
        Open a raw MySQLdb connection and return (conn, None), or
        (None, error_string) on failure.

        The connection is intentionally NOT tied to Django's
        configured database name — the whole point is to run DDL
        that targets schemas which may not exist yet.
        """
        try:
            import MySQLdb
        except ImportError:
            return None, 'mysqlclient is not installed'

        try:
            conn = MySQLdb.connect(
                host=host,
                port=port,
                user=user,
                password=password or '',
                connect_timeout=5,
            )
            return conn, None
        except MySQLdb.OperationalError as exc:
            return None, str(exc)

    def _print_db_setup_failure(self, *, admin_user, db_host, db_port,
                                db_name, db_user, grant_host, error):
        """
        Print the specific error plus concrete remediation steps.

        The remediation block below used to interpolate the live
        DB_PASSWORD value into the `IDENTIFIED BY '...'` line. That
        is a plaintext secret written to stderr, and on a CI or
        script-driven bootstrap stderr is typically redirected to a
        log file that outlives the process. The line below uses a
        placeholder instead and tells the operator where the value
        lives. If the operator wants to run the SQL manually, they
        substitute from backend/.env themselves.
        """
        w = self.stderr.write
        w('')
        w(self.style.ERROR(
            f'[db-setup] could not connect to MariaDB at '
            f'{db_host}:{db_port} as {admin_user!r}'
        ))
        if error:
            w(self.style.ERROR(f'[db-setup]   error: {error}'))
        w('')
        w(self.style.WARNING('[db-setup] options to fix this:'))
        w('')
        w('  1. Set the admin credentials in backend/.env (preferred —')
        w('     not visible in `ps` or shell history):')
        w('     DB_ADMIN_USER=root')
        w('     DB_ADMIN_PASSWORD=yourpass')
        w('')
        w('  2. Or, for one-off/CI use, pass them on the command line.')
        w('     WARNING: a value passed here is visible to every user')
        w('     on the host via `ps` and is recorded in shell history.')
        w(self.style.WARNING(
            "     python manage.py bootstrap --db-admin-user root "
            "--db-admin-password 'yourpass'"
        ))
        w('')
        w('  3. On a Debian/Ubuntu default install, run with sudo so')
        w('     the Unix-socket auth path is available:')
        w(self.style.WARNING(
            '     sudo python manage.py bootstrap'
        ))
        w('')
        w('  4. Or provision the database manually and re-run with')
        w('     --no-db-setup. Substitute the DB_PASSWORD value from')
        w('     backend/.env into the IDENTIFIED BY clause below:')
        w('')
        w(f"     mysql -u {admin_user} -p <<'SQL'")
        w(f"       CREATE DATABASE IF NOT EXISTS `{db_name}`")
        w(f"         CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        w(f"       CREATE USER IF NOT EXISTS '{db_user}'@'{grant_host}'")
        w(f"         IDENTIFIED BY '<DB_PASSWORD from backend/.env>';")
        w(f"       GRANT ALL PRIVILEGES ON `{db_name}`.*")
        w(f"         TO '{db_user}'@'{grant_host}';")
        w(f"       FLUSH PRIVILEGES;")
        w(f'     SQL')
        w('')
        w(f"     python manage.py bootstrap --no-db-setup")

    # ── Arabic font check ────────────────────────────────────────────

    def _verify_arabic_font(self):
        """
        Confirm the Arabic TTF the PDF exporter embeds is reachable.

        Not fatal — the PDF is still generated without it, using a
        system fallback font. But the visual output will not match
        the app's typography, and it is much better to know this at
        bootstrap time than the first time an admin clicks "PDF".
        """
        font_path = resolve_arabic_font_path()
        if font_path is not None:
            self.stdout.write(self.style.SUCCESS(
                f'[font] Arabic TTF found: {font_path}'
            ))
            return True

        self.stdout.write(self.style.WARNING(
            '[font] Arabic TTF not found. PDF export will fall back to a '
            'system font (if any) and the output will not match the app.'
        ))
        self.stdout.write(self.style.WARNING(
            '[font] To fix, copy the file into one of these paths:'
        ))
        for candidate in arabic_font_candidates():
            self.stdout.write(self.style.WARNING(
                f'         {candidate}'
            ))
        return False

    # ── Summary ──────────────────────────────────────────────────────

    def _print_summary(self, options):
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            '═══════════════════════════════════════════════════════════'
        ))
        self.stdout.write(self.style.SUCCESS('  ✓ Bootstrap complete.'))
        self.stdout.write(self.style.SUCCESS(
            '═══════════════════════════════════════════════════════════'
        ))
        self.stdout.write('')
        self.stdout.write('  Next steps:')
        self.stdout.write('')
        self.stdout.write('    Backend dev server:')
        self.stdout.write('      python manage.py runserver 0.0.0.0:5004')
        self.stdout.write('')
        self.stdout.write('    Frontend dev server (in another terminal):')
        self.stdout.write('      cd frontend && pnpm dev')
        self.stdout.write('')
        self.stdout.write('    Admin credentials:')
        self.stdout.write(
            '      If seed_data just created the admin account, its '
            'one-time password was printed to stderr above.'
        )
        self.stdout.write(
            '      Lost it? Reset with:  python manage.py reset_admin_password'
        )
        self.stdout.write('')
        self.stdout.write('    Health check:')
        self.stdout.write('      python manage.py doctor')
        self.stdout.write('')

    # ── --clean helpers ─────────────────────────────────────────────

    def _confirm_clean(self, assume_yes):
        if assume_yes:
            return

        db = settings.DATABASES['default']
        engine = db['ENGINE']

        self.stdout.write('')
        self.stdout.write(self.style.WARNING('⚠  --clean will destroy:'))
        if engine.endswith('sqlite3'):
            self.stdout.write(self.style.WARNING(f"   • {db['NAME']}"))
        else:
            host = db.get('HOST') or 'localhost'
            port = db.get('PORT') or '3306'
            self.stdout.write(self.style.WARNING(
                f"   • database `{db['NAME']}` on {host}:{port}"
            ))
        for app in MODEL_APPS:
            self.stdout.write(self.style.WARNING(
                f'   • apps/{app}/migrations/*.py  (except __init__.py)'
            ))
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            '   This cannot be undone. Any questions, users, results, '
            'bookmarks, history, and settings will be lost.'
        ))
        self.stdout.write('')

        try:
            answer = input("Type DELETE (all caps) to proceed: ").strip()
        except (EOFError, KeyboardInterrupt):
            self.stdout.write('')
            raise CommandError('Aborted.')

        if answer != 'DELETE':
            raise CommandError('Aborted.')

    def _clean(self, apps_dir):
        from django.db import connection

        if connection.vendor == 'sqlite':
            self._clean_sqlite(apps_dir)
        elif connection.vendor == 'mysql':
            self._clean_mariadb(apps_dir)
        else:
            raise CommandError(
                f'--clean does not support this database backend: '
                f'{connection.vendor}. Supported: sqlite3, mysql/mariadb.'
            )

        self._wipe_migrations(apps_dir)

    # ── MariaDB (active) ─────────────────────────────────────────────

    def _clean_mariadb(self, apps_dir):
        """
        Drop every table in the configured schema.

        Uses information_schema so views and any table created outside
        the Django migration graph are also caught. Foreign key checks
        are disabled for the duration of the drop batch.
        """
        from django.db import connections

        # Close any half-open transaction from an earlier command in
        # this process — it holds a metadata lock that would block
        # DROP TABLE.
        connections.close_all()

        db_name = settings.DATABASES['default']['NAME']

        with connections['default'].cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s",
                [db_name],
            )
            tables = [row[0] for row in cursor.fetchall()]

            if tables:
                cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                for table in tables:
                    cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
                cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
                self.stdout.write(
                    f'[clean] dropped {len(tables)} table(s) '
                    f'from `{db_name}`'
                )
            else:
                self.stdout.write(
                    f'[clean] database `{db_name}` is already empty'
                )

    # ── SQLite (commented out — kept for reference) ──────────────────
    #
    # def _clean_sqlite(self, apps_dir):
    #     from django.db import connections
    #
    #     engine = settings.DATABASES['default']['ENGINE']
    #     if engine != 'django.db.backends.sqlite3':
    #         raise CommandError(
    #             f'_clean_sqlite called with non-SQLite engine: {engine}'
    #         )
    #
    #     # CRITICAL: close every open DB connection before unlinking
    #     # the SQLite file. Do NOT remove this call. It is load-bearing.
    #     connections.close_all()
    #
    #     db_path = Path(settings.DATABASES['default']['NAME'])
    #     deleted_any = False
    #     for suffix in ('', '-journal', '-wal', '-shm'):
    #         candidate = Path(str(db_path) + suffix)
    #         if candidate.exists():
    #             candidate.unlink()
    #             self.stdout.write(f'[clean] deleted {candidate}')
    #             deleted_any = True
    #     if not deleted_any:
    #         self.stdout.write(
    #             f'[clean] no database file at {db_path} (already clean)'
    #         )

    # ── Shared: wipe migration files ─────────────────────────────────

    def _wipe_migrations(self, apps_dir):
        """
        Delete every migration file under each model app, keeping
        __init__.py so the folder remains a Python package, and drop
        the __pycache__ so Django does not pick up stale compiled
        migrations on the next import.
        """
        for app in MODEL_APPS:
            migrations_dir = apps_dir / app / 'migrations'
            if not migrations_dir.is_dir():
                continue
            for f in sorted(migrations_dir.glob('*.py')):
                if f.name == '__init__.py':
                    continue
                f.unlink()
                self.stdout.write(
                    f'[clean] deleted apps/{app}/migrations/{f.name}'
                )
            pycache = migrations_dir / '__pycache__'
            if pycache.is_dir():
                shutil.rmtree(pycache)

    # ── main bootstrap pipeline ─────────────────────────────────────

    def _bootstrap(self, apps_dir, options):
        # ── Ensure every custom app has a migrations package ─────
        ensured = []
        for app in CUSTOM_APPS:
            app_dir = apps_dir / app
            if not app_dir.is_dir():
                continue
            migrations_dir = app_dir / 'migrations'
            migrations_dir.mkdir(exist_ok=True)
            init_file = migrations_dir / '__init__.py'
            if not init_file.exists():
                init_file.touch()
            ensured.append(app)

        self.stdout.write(self.style.SUCCESS(
            f'[3/N] migrations packages ensured: '
            f'{", ".join(ensured) or "(none found)"}'
        ))

        # ── makemigrations only if any model app lacks migration files ─
        needs_makemigrations = False
        for app in MODEL_APPS:
            migrations_dir = apps_dir / app / 'migrations'
            has_file = any(
                f.name != '__init__.py' for f in migrations_dir.glob('*.py')
            )
            if not has_file:
                needs_makemigrations = True
                break

        if needs_makemigrations:
            self.stdout.write(
                '[4/N] creating initial migrations for custom apps…'
            )
            call_command('makemigrations', *MODEL_APPS)
        else:
            self.stdout.write(
                '[4/N] custom apps already have migrations — '
                'skipping makemigrations'
            )

        # ── migrate (Django makes this a no-op if nothing new) ───
        self.stdout.write('[5/N] applying migrations…')
        call_command('migrate')

        # ── seed (idempotent: exists() / get_or_create) ──────────
        if options.get('skip_seed'):
            self.stdout.write('[6/N] seeding skipped (--skip-seed)')
            return

        self.stdout.write('[6/N] seeding default data…')
        seed_kwargs = {
            'with_sample_questions': options['with_sample_questions'],
        }
        if options.get('reset_capabilities'):
            seed_kwargs['reset'] = True
        call_command('seed', **seed_kwargs)