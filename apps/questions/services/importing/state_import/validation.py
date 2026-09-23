# backend/apps/questions/services/importing/state_import/validation.py
"""
Envelope shape validation for the state-import path.
"""
import base64
import binascii
from datetime import datetime
from uuid import UUID

from django.conf import settings

from ..validators import _is_valid_uuid, MAX_CHOICES
from apps.questions.image_policy import MAX_IMAGE_SIZE
from .constants import STATE_FORMAT, STATE_FORMAT_VERSION
from ....models import (
    CATEGORY_NAME_MAX_LENGTH,
    TAG_NAME_MAX_LENGTH,
    CASE_GROUP_MAX_LENGTH,
    CASE_STEM_MAX_LENGTH,
    QUESTION_TEXT_MAX_LENGTH,
    CHOICE_TEXT_MAX_LENGTH,
    EXPLANATION_TEXT_MAX_LENGTH,
)


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

    meta = payload.get('meta')
    if not isinstance(meta, dict):
        return {'error': "الحقل 'meta' غير صالح", 'code': 400}
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
        if key not in payload or not isinstance(payload.get(key), list):
            return {'error': f"الحقل '{key}' غير صالح", 'code': 400}

    question_limit = settings.MAX_STATE_IMPORT_QUESTIONS
    if len(payload['questions']) > question_limit:
        return {
            'error': (
                f"عدد الأسئلة يتجاوز الحد ({question_limit})"
            ),
            'code': 400,
        }

    def _uuid_text(raw):
        if raw is None:
            return ''
        return str(raw).strip()

    def _canonical_uuid(raw):
        text = _uuid_text(raw)
        return str(UUID(text)) if text else ''

    def _check_uuids(entries, field, label, *, required=True):
        seen = set()
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                return f'{label}: العنصر رقم {idx + 1} ليس كائناً'
            uuid_str = _uuid_text(entry.get(field))
            if required and not uuid_str:
                return f'{label}: uuid مطلوب في العنصر رقم {idx + 1}'
            if uuid_str and not _is_valid_uuid(uuid_str):
                return (
                    f'{label}: uuid غير صالح في العنصر رقم {idx + 1} '
                    f'({uuid_str!r})'
                )
            canonical = _canonical_uuid(uuid_str)
            if canonical in seen:
                return f'{label}: uuid مكرر في العنصر رقم {idx + 1}'
            seen.add(canonical)
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

    natural_keys = (
        ('categories', 'name', 'التصنيفات', CATEGORY_NAME_MAX_LENGTH),
        ('tags', 'name', 'الوسوم', TAG_NAME_MAX_LENGTH),
        ('cases', 'key', 'الحالات', CASE_GROUP_MAX_LENGTH),
    )
    for section, field, label, max_length in natural_keys:
        seen = set()
        for idx, entry in enumerate(payload[section]):
            raw = entry.get(field)
            if not isinstance(raw, str) or not raw.strip():
                return {
                    'error': f'{label}: {field} مطلوب في العنصر رقم {idx + 1}',
                    'code': 400,
                }
            value = raw.strip()
            if len(value) > max_length:
                return {
                    'error': f'{label}: {field} يتجاوز {max_length} حرفاً في العنصر رقم {idx + 1}',
                    'code': 400,
                }
            folded = value.casefold()
            if folded in seen:
                return {
                    'error': f'{label}: {field} مكرر في العنصر رقم {idx + 1}',
                    'code': 400,
                }
            seen.add(folded)

    for idx, case in enumerate(payload['cases']):
        stem = case.get('stem') or ''
        if not isinstance(stem, str) or len(stem) > CASE_STEM_MAX_LENGTH:
            return {'error': f'الحالات: stem غير صالح في العنصر رقم {idx + 1}', 'code': 400}
        title = case.get('title') or ''
        if not isinstance(title, str) or len(title) > 200:
            return {'error': f'الحالات: title غير صالح في العنصر رقم {idx + 1}', 'code': 400}
        for field in ('authored_by_uuid',):
            value = _uuid_text(case.get(field))
            if value and not _is_valid_uuid(value):
                return {'error': f'الحالات: {field} غير صالح في العنصر رقم {idx + 1}', 'code': 400}
        author_name = case.get('authored_by_name')
        if author_name is not None and not isinstance(author_name, str):
            return {'error': f'الحالات: authored_by_name غير صالح في العنصر رقم {idx + 1}', 'code': 400}

    for idx, category in enumerate(payload['categories']):
        description = category.get('description') or ''
        color = category.get('color') or ''
        icon = category.get('icon') or ''
        if not isinstance(description, str):
            return {'error': f'التصنيفات: description غير صالح في العنصر رقم {idx + 1}', 'code': 400}
        if not isinstance(color, str) or len(color) > 20:
            return {'error': f'التصنيفات: color غير صالح في العنصر رقم {idx + 1}', 'code': 400}
        if not isinstance(icon, str) or len(icon) > 50:
            return {'error': f'التصنيفات: icon غير صالح في العنصر رقم {idx + 1}', 'code': 400}

    for idx, entry in enumerate(payload.get('questions') or []):
        if not isinstance(entry, dict):
            continue
        for field, label in (
            ('category_uuid', 'التصنيف'),
            ('case_uuid', 'الحالة'),
            ('authored_by_uuid', 'المؤلف'),
            ('owned_by_uuid', 'المالك'),
        ):
            uuid_str = _uuid_text(entry.get(field))
            if uuid_str and not _is_valid_uuid(uuid_str):
                return {
                    'error': (
                        f'الأسئلة: uuid {label} غير صالح في السؤال '
                        f'رقم {idx + 1} ({uuid_str!r})'
                    ),
                    'code': 400,
                }
        tags = entry.get('tags')
        if not isinstance(tags, list):
            return {
                'error': f'الأسئلة: الوسوم يجب أن تكون قائمة في السؤال رقم {idx + 1}',
                'code': 400,
            }
        for tag_uuid in tags:
            if tag_uuid and not _is_valid_uuid(str(tag_uuid)):
                return {
                    'error': (
                        f'الأسئلة: uuid وسم غير صالح في السؤال رقم '
                        f'{idx + 1} ({tag_uuid!r})'
                    ),
                    'code': 400,
                }

        question = entry.get('question')
        choices = entry.get('choices')
        if not isinstance(question, str) or not question.strip():
            return {'error': f'السؤال رقم {idx + 1}: نص السؤال مطلوب', 'code': 400}
        if len(question.strip()) > QUESTION_TEXT_MAX_LENGTH:
            return {'error': f'السؤال رقم {idx + 1}: نص السؤال طويل جداً', 'code': 400}
        if not isinstance(choices, list) or not 2 <= len(choices) <= MAX_CHOICES:
            return {
                'error': f'السؤال رقم {idx + 1}: عدد الاختيارات غير صالح',
                'code': 400,
            }
        if any(not isinstance(choice, str) for choice in choices):
            return {'error': f'السؤال رقم {idx + 1}: الاختيارات يجب أن تكون نصوصاً', 'code': 400}
        cleaned_choices = [choice.strip() for choice in choices]
        if any(not choice for choice in cleaned_choices):
            return {'error': f'السؤال رقم {idx + 1}: يوجد اختيار فارغ', 'code': 400}
        if any(len(choice) > CHOICE_TEXT_MAX_LENGTH for choice in cleaned_choices):
            return {'error': f'السؤال رقم {idx + 1}: نص الاختيار طويل جداً', 'code': 400}
        if len({choice.casefold() for choice in cleaned_choices}) != len(cleaned_choices):
            return {'error': f'السؤال رقم {idx + 1}: الاختيارات مكررة', 'code': 400}
        try:
            if isinstance(entry.get('correct_answer'), bool):
                raise ValueError
            numeric_answer = float(entry.get('correct_answer'))
            if not numeric_answer.is_integer():
                raise ValueError
            correct_answer = int(numeric_answer)
        except (TypeError, ValueError, OverflowError):
            return {'error': f'السؤال رقم {idx + 1}: رقم الإجابة غير صالح', 'code': 400}
        if not 1 <= correct_answer <= len(cleaned_choices):
            return {'error': f'السؤال رقم {idx + 1}: رقم الإجابة خارج النطاق', 'code': 400}
        for boolean_field in ('is_draft', 'verified'):
            if boolean_field in entry and not isinstance(entry[boolean_field], bool):
                return {'error': f'السؤال رقم {idx + 1}: {boolean_field} يجب أن يكون منطقياً', 'code': 400}
        verified_at = entry.get('verified_at')
        if verified_at:
            try:
                datetime.fromisoformat(str(verified_at).replace('Z', '+00:00'))
            except ValueError:
                return {'error': f'السؤال رقم {idx + 1}: verified_at غير صالح', 'code': 400}
        explanation = entry.get('explanation') or ''
        if not isinstance(explanation, str) or len(explanation) > EXPLANATION_TEXT_MAX_LENGTH:
            return {'error': f'السؤال رقم {idx + 1}: الشرح غير صالح', 'code': 400}

        for field, max_length in (
            ('source', 200),
            ('verified_by', 80),
        ):
            value = entry.get(field) or ''
            if not isinstance(value, str) or len(value) > max_length:
                return {'error': f'السؤال رقم {idx + 1}: {field} غير صالح', 'code': 400}
        verification_notes = entry.get('verification_notes')
        if verification_notes is not None and not isinstance(verification_notes, str):
            return {'error': f'السؤال رقم {idx + 1}: verification_notes غير صالح', 'code': 400}
        difficulty = entry.get('difficulty', 'medium')
        if not isinstance(difficulty, str) or difficulty not in {'easy', 'medium', 'hard'}:
            return {'error': f'السؤال رقم {idx + 1}: difficulty غير صالح', 'code': 400}

        case_order = entry.get('case_order')
        if case_order is not None and (
            not isinstance(case_order, int)
            or isinstance(case_order, bool)
            or case_order < 1
        ):
            return {'error': f'السؤال رقم {idx + 1}: case_order غير صالح', 'code': 400}

        for field in ('authored_by_name', 'owned_by_name'):
            value = entry.get(field)
            if value is not None and not isinstance(value, str):
                return {'error': f'السؤال رقم {idx + 1}: {field} غير صالح', 'code': 400}

        image = entry.get('image')
        if image is not None:
            if not isinstance(image, dict):
                return {'error': f'السؤال رقم {idx + 1}: image غير صالح', 'code': 400}
            filename = image.get('filename')
            mime = image.get('mime')
            encoded = image.get('data_base64')
            if not isinstance(filename, str) or not filename.strip():
                return {'error': f'السؤال رقم {idx + 1}: اسم الصورة غير صالح', 'code': 400}
            if not isinstance(mime, str) or not mime.startswith('image/'):
                return {'error': f'السؤال رقم {idx + 1}: نوع الصورة غير صالح', 'code': 400}
            if not isinstance(encoded, str):
                return {'error': f'السؤال رقم {idx + 1}: بيانات الصورة غير صالحة', 'code': 400}
            try:
                image_bytes = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                return {'error': f'السؤال رقم {idx + 1}: بيانات الصورة غير صالحة', 'code': 400}
            if len(image_bytes) > MAX_IMAGE_SIZE:
                return {'error': f'السؤال رقم {idx + 1}: حجم الصورة يتجاوز الحد', 'code': 400}

    category_uuids = {_canonical_uuid(item['uuid']) for item in payload['categories']}
    tag_uuids = {_canonical_uuid(item['uuid']) for item in payload['tags']}
    case_uuids = {_canonical_uuid(item['uuid']) for item in payload['cases']}
    for idx, tag in enumerate(payload['tags']):
        parent_uuid = _uuid_text(tag.get('parent_uuid'))
        if parent_uuid:
            if not _is_valid_uuid(parent_uuid):
                return {'error': f'الوسوم: parent_uuid غير صالح في العنصر رقم {idx + 1}', 'code': 400}
            parent_uuid = _canonical_uuid(parent_uuid)
            if parent_uuid not in tag_uuids:
                return {'error': f'الوسوم: الوسم الأب غير موجود في الحزمة', 'code': 400}
    for idx, entry in enumerate(payload['questions']):
        references = (
            ('category_uuid', category_uuids, 'التصنيف'),
            ('case_uuid', case_uuids, 'الحالة'),
        )
        for field, available, label in references:
            value = _uuid_text(entry.get(field))
            if value:
                value = _canonical_uuid(value)
            if value and value not in available:
                return {'error': f'السؤال رقم {idx + 1}: {label} المرجعي غير موجود في الحزمة', 'code': 400}
        for tag_uuid in entry['tags']:
            if _canonical_uuid(tag_uuid) not in tag_uuids:
                return {'error': f'السؤال رقم {idx + 1}: وسم مرجعي غير موجود في الحزمة', 'code': 400}
        canonical_tags = [_canonical_uuid(tag_uuid) for tag_uuid in entry['tags']]
        if len(canonical_tags) != len(set(canonical_tags)):
            return {'error': f'السؤال رقم {idx + 1}: يوجد وسم مكرر', 'code': 400}

    return None
