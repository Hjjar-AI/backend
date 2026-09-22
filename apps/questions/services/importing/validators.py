# backend/apps/questions/services/importing/validators.py
"""
Shared validation constants and helpers for the import sub-modules.

This module has no dependencies on the sibling import modules, so it
can be imported from anywhere in the package without creating a
cycle.

Two things live here that used to be `@staticmethod` on
`ImportService`:

  • `verify_upload_mime` — used by flat_import, telegram_import,
    and state_import, each of which uploads a file.
  • `get_user_safe`      — used by the same three modules to resolve
    the acting username to a User row.

`VALID_DIFFICULTIES` is derived from `Question.DIFFICULTY_CHOICES`
rather than being a hardcoded literal, so the import path cannot
silently misclassify data if a difficulty tier is ever added or
renamed on the model. The previous literal `{'easy', 'medium', 'hard'}`
was duplicated in `seed_sample_questions.py`; both files now derive
from the model.
"""

import logging
import uuid as uuid_module

from django.conf import settings

import pandas as pd

from apps.questions.models import Question

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────

MAX_CHOICES = getattr(settings, 'MAX_CHOICES', 8)

ALLOWED_UPLOAD_MIMES = frozenset({
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-excel',
    'text/csv',
    'application/json',
    'text/plain',
})

# Bound for a JSON array import (flat JSON list, Telegram message
# array). Prevents a multi-gigabyte payload from exhausting memory
# before the MAX_IMPORT_QUESTIONS check downstream.
MAX_JSON_IMPORT_ELEMENTS = 10000

# Derived from the model so the two cannot drift. `parse_difficulty()`
# coerces anything not in this set to 'medium' — a decision that is
# only safe if the set matches the model's actual choice keys.
VALID_DIFFICULTIES = frozenset(c for c, _ in Question.DIFFICULTY_CHOICES)


# ── Value coercion ─────────────────────────────────────────────────────

def _cell_to_str(value):
    """Coerce a spreadsheet cell to a stripped string, or '' for NaN/None."""
    if value is None:
        return ''
    try:
        if pd.isna(value):
            return ''
    except (TypeError, ValueError):
        pass
    return str(value)


def _is_valid_uuid(value):
    """Return True iff `value` is a well-formed UUID string."""
    if not value:
        return False
    try:
        uuid_module.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def parse_difficulty(value):
    """Normalize a difficulty string to one of easy / medium / hard."""
    value = str(value).lower().strip()
    if value not in VALID_DIFFICULTIES:
        value = 'medium'
    return value


# ── Upload MIME check ─────────────────────────────────────────────────

def verify_upload_mime(file):
    """
    Reject an upload whose MIME type is not in ALLOWED_UPLOAD_MIMES.

    Returns None on success, or a dict {'error': ..., 'code': ...} on
    failure. The caller checks `if mime_error is not None: return
    mime_error` — matching the shape of every other error return in
    the import flow.

    Fails closed if python-magic is unavailable: the check is a
    defense against polyglot files whose extension lies about their
    content, and running without it would silently disable that
    defense.
    """
    try:
        import magic
    except ImportError:
        logger.critical(
            'python-magic is not installed; refusing upload because '
            'the MIME check cannot run.'
        )
        return {
            'error': 'تعذر التحقق من نوع الملف (الخادم غير مهيأ). تواصل مع المسؤول.',
            'code': 500,
        }

    try:
        head = file.read(1024)
        file.seek(0)
        mime = magic.from_buffer(head, mime=True)
    except Exception as e:
        logger.exception('MIME verification failed for upload: %s', e)
        try:
            file.seek(0)
        except Exception:
            pass
        return {'error': 'تعذر التحقق من نوع الملف', 'code': 400}

    if mime not in ALLOWED_UPLOAD_MIMES:
        logger.warning(
            'Rejected upload with disallowed MIME type %r '
            '(filename=%r, size=%r)',
            mime, getattr(file, 'name', '<unknown>'),
            getattr(file, 'size', None),
        )
        return {'error': 'نوع الملف غير صالح', 'code': 400}

    return None


# ── User lookup ───────────────────────────────────────────────────────

def get_user_safe(username):
    """Return the User with the given username, or None."""
    from apps.users.models import User
    return User.objects.filter(username=username).first()