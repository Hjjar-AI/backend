# backend/apps/questions/services/importing/state_import/validation.py
"""
Envelope shape validation for the state-import path.
"""
from django.conf import settings

from ..validators import _is_valid_uuid
from .constants import STATE_FORMAT, STATE_FORMAT_VERSION


def _validate_state_envelope(payload):
    """
    Return None if the payload is a valid v2 envelope, or a
    {'error': ..., 'code': ...} dict otherwise.

    Validates:
      • meta.format and meta.version identifiers
      • top-level sections are lists
      • question count is under MAX_IMPORT_QUESTIONS
      • every uuid field is well-formed (a malformed uuid would
        otherwise silently create a new row that can never be matched
        on the next import)
    """
    if not isinstance(payload, dict):
        return {'error': 'صيغة الملف غير صالحة', 'code': 400}

    meta = payload.get('meta') or {}
    if meta.get('format') != STATE_FORMAT:
        return {'error': 'الملف ليس حزمة بيانات أسئلة مُختبِر', 'code': 400}

    version = meta.get('version')
    if version != STATE_FORMAT_VERSION:
        return {
            'error': (
                f'إصدار الحزمة ({version}) غير مدعوم. الإصدار '
                f'المطلوب: {STATE_FORMAT_VERSION}. أعد التصدير من '
                f'الإصدار الحالي.'
            ),
            'code': 400,
        }

    for key in ('categories', 'tags', 'cases', 'questions'):
        if not isinstance(payload.get(key, []), list):
            return {'error': f"الحقل '{key}' غير صالح", 'code': 400}

    if len(payload['questions']) > settings.MAX_IMPORT_QUESTIONS:
        return {
            'error': (
                f"عدد الأسئلة يتجاوز الحد ({settings.MAX_IMPORT_QUESTIONS})"
            ),
            'code': 400,
        }

    def _check_uuids(entries, field, label):
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                return f'{label}: العنصر رقم {idx + 1} ليس كائناً'
            uuid_str = (entry.get(field) or '').strip()
            if uuid_str and not _is_valid_uuid(uuid_str):
                return (
                    f'{label}: uuid غير صالح في العنصر رقم {idx + 1} '
                    f'({uuid_str!r})'
                )
        return None

    for entries, field, label in (
        (payload.get('categories') or [], 'uuid', 'التصنيفات'),
        (payload.get('tags') or [], 'uuid', 'الوسوم'),
        (payload.get('cases') or [], 'uuid', 'الحالات'),
        (payload.get('questions') or [], 'uuid', 'الأسئلة'),
    ):
        msg = _check_uuids(entries, field, label)
        if msg is not None:
            return {'error': msg, 'code': 400}

    for idx, entry in enumerate(payload.get('questions') or []):
        if not isinstance(entry, dict):
            continue
        for field, label in (
            ('category_uuid', 'التصنيف'),
            ('case_uuid', 'الحالة'),
            ('authored_by_uuid', 'المؤلف'),
            ('owned_by_uuid', 'المالك'),
        ):
            uuid_str = (entry.get(field) or '').strip()
            if uuid_str and not _is_valid_uuid(uuid_str):
                return {
                    'error': (
                        f'الأسئلة: uuid {label} غير صالح في السؤال '
                        f'رقم {idx + 1} ({uuid_str!r})'
                    ),
                    'code': 400,
                }
        for tag_uuid in entry.get('tags') or []:
            if tag_uuid and not _is_valid_uuid(str(tag_uuid)):
                return {
                    'error': (
                        f'الأسئلة: uuid وسم غير صالح في السؤال رقم '
                        f'{idx + 1} ({tag_uuid!r})'
                    ),
                    'code': 400,
                }

    return None