# backend/apps/questions/utils.py
"""
SQLite-only full-text-search helpers.

These use SQLite's FTS5 virtual-table extension. On any other
backend (MariaDB / MySQL / Postgres) they must not be called —
there is no `fts5` module and `CREATE VIRTUAL TABLE` behaves
differently. Each entry point asserts the backend first so a
future caller on MariaDB fails with a clear message instead of a
syntax error from deep inside the DB driver.

No caller exists today. These are kept as a scaffold for a future
SQLite deployment; if you do not intend to support SQLite, delete
the file.
"""
from django.core.exceptions import ImproperlyConfigured
from django.db import connection


def _require_sqlite():
    if connection.vendor != 'sqlite':
        raise ImproperlyConfigured(
            f'apps.questions.utils requires a SQLite backend '
            f'(got vendor={connection.vendor!r}). These helpers use '
            f'SQLite FTS5 and are not portable.'
        )


def ensure_fts_table():
    _require_sqlite()
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts "
            "USING fts5(question, explanation, tags, "
            "content='questions_question', content_rowid='id', "
            "tokenize='unicode61')"
        )


def rebuild_fts_index():
    _require_sqlite()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO questions_fts(questions_fts) VALUES('rebuild')"
        )


def search_questions_fts(query):
    _require_sqlite()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rowid FROM questions_fts "
            "WHERE questions_fts MATCH %s",
            [query],
        )
        rows = cursor.fetchall()
        return [row[0] for row in rows]