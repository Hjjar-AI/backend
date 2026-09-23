# backend/apps/questions/services/importing/flat_import.py
"""
Flat import path: XLSX / XLS / CSV / JSON lists of questions.

Each row carries the question text, a `choice_1..choice_N` set of
columns (or, for JSON, a `choices` list), a correct_answer index, and
optional metadata (category, tags, difficulty, case_group/case_stem).

This module owns the portable flat-file import contract. It accepts the
legacy columns as well as the stable UUID/name and lossless tag fields emitted
by the current exporter.
"""

import hashlib
import json
import logging
import uuid as uuid_module
from pathlib import Path

import pandas as pd

from django.conf import settings
from django.db import transaction

from ...models import (
    Question,
    Category,
    Tag,
    ClinicalCase,
    KnowledgeObject,
    clean_tag_name,
    CATEGORY_NAME_MAX_LENGTH,
    QUESTION_TEXT_MAX_LENGTH,
    EXPLANATION_TEXT_MAX_LENGTH,
    CHOICE_TEXT_MAX_LENGTH,
    CASE_STEM_MAX_LENGTH,
    CASE_GROUP_MAX_LENGTH,
)
from ...validation import (
    clean_and_validate_choices,
    validate_correct_answer,
)
from ...translation_validation import normalize_translations

from .validators import (
    MAX_CHOICES,
    MAX_JSON_IMPORT_ELEMENTS,
    _cell_to_str,
    get_user_safe,
    parse_difficulty,
    verify_upload_mime,
)
from .staging import stage_upload, cleanup_staged_upload
from ..content_quality import (
    flag_import_quality_issues,
    prepare_import_content,
    quality_key,
)

logger = logging.getLogger(__name__)


# ── Row-level helpers ──────────────────────────────────────────────────

def _resolve_category(row):
    """
    Resolve a row's category reference to a Category.id, or None.

    Prefers a portable UUID, then a category name, and finally accepts a local
    integer id when it actually exists. A miss is not an error — the question
    is imported with category=None, which is a legal state.
    """
    category_name = (
        _cell_to_str(row.get('category_name')).strip()
        or _cell_to_str(row.get('category')).strip()
    )
    if len(category_name) > CATEGORY_NAME_MAX_LENGTH:
        raise ValueError(
            f'اسم التصنيف يتجاوز الحد الأقصى ({CATEGORY_NAME_MAX_LENGTH} حرفاً)'
        )
    category_uuid_raw = _cell_to_str(row.get('category_uuid')).strip()
    if category_uuid_raw:
        try:
            category_uuid = uuid_module.UUID(category_uuid_raw)
        except (ValueError, TypeError, AttributeError):
            raise ValueError('uuid التصنيف غير صالح')
        category = Category.objects.filter(uuid=category_uuid).first()
        if category is not None:
            return category.id
        if category_name:
            category = Category.objects.filter(name=category_name).first()
            if category is None:
                category = Category.objects.create(
                    uuid=category_uuid,
                    name=category_name,
                )
            return category.id

    if category_name:
        category = Category.objects.filter(name=category_name).first()
        if category is not None:
            return category.id

    category_id_raw = _cell_to_str(row.get('category_id')).strip()
    if category_id_raw:
        try:
            numeric_id = float(category_id_raw)
            category_id = int(numeric_id) if numeric_id.is_integer() else None
        except (ValueError, TypeError):
            category_id = None
        if category_id is not None and Category.objects.filter(id=category_id).exists():
            return category_id

    return None


def _extract_choices(row):
    """
    Extract a row's choices as a list of strings.

    Three accepted shapes, checked in this order:
      1. `choices` is already a list (JSON path).
      2. `choices` is a JSON-encoded string (CSV/Excel cell holding
         a JSON array).
      3. A set of `choice_1..choice_N` columns (the primary Excel
         layout).
    """
    if 'choices' in row and isinstance(row['choices'], list):
        if any(not isinstance(choice, str) for choice in row['choices']):
            raise ValueError('يجب أن تكون جميع الاختيارات نصوصاً')
        return row['choices']
    if 'choices' in row and isinstance(row['choices'], str):
        try:
            parsed = json.loads(row['choices'])
            if isinstance(parsed, list):
                if any(not isinstance(choice, str) for choice in parsed):
                    raise ValueError('يجب أن تكون جميع الاختيارات نصوصاً')
                return parsed
        except json.JSONDecodeError:
            pass

    choices = []
    for i in range(1, MAX_CHOICES + 1):
        key = f'choice_{i}'
        if key in row:
            val = _cell_to_str(row.get(key)).strip()
            if val:
                choices.append(val)
    return choices


def _resolve_case_from_row(row, author=None):
    """
    Resolve the row's case_group / case_stem into a ClinicalCase.

    Returns None when the row carries neither field. When only a stem
    is present, a deterministic `auto-<sha256[:12]>` key is derived
    from the stem text so the same stem always resolves to the same
    case across imports.
    """
    nested = row.get('case') if isinstance(row.get('case'), dict) else {}
    case_key = (
        _cell_to_str(row.get('case_key')).strip()
        or _cell_to_str(row.get('case_group')).strip()
        or _cell_to_str(nested.get('key')).strip()
    )[:CASE_GROUP_MAX_LENGTH]

    case_stem = (
        _cell_to_text_stem(row)
        or _cell_to_str(nested.get('stem')).strip()[:CASE_STEM_MAX_LENGTH]
    )

    if not case_key and not case_stem:
        return None

    if not case_key:
        digest = hashlib.sha256(case_stem.encode('utf-8')).hexdigest()[:12]
        case_key = f'auto-{digest}'

    raw_uuid = _cell_to_str(row.get('case_uuid')).strip() or _cell_to_str(nested.get('uuid')).strip()
    case_uuid = None
    if raw_uuid:
        try:
            case_uuid = uuid_module.UUID(raw_uuid)
        except (ValueError, TypeError, AttributeError):
            raise ValueError('uuid الحالة غير صالح')

    case = ClinicalCase.objects.filter(uuid=case_uuid).first() if case_uuid else None
    created = False
    if case is None:
        defaults = {
            'stem': case_stem or None,
            'title': (
                _cell_to_str(row.get('case_title')).strip()
                or _cell_to_str(nested.get('title')).strip()
            )[:200] or None,
            'authored_by': author,
        }
        if case_uuid:
            defaults['uuid'] = case_uuid
        case, created = ClinicalCase.objects.get_or_create(
            key=case_key,
            defaults=defaults,
        )

    if not created and case_stem and not case.stem:
        case.stem = case_stem
        case.save(update_fields=['stem', 'updated_at'])

    return case


def _cell_to_text_stem(row):
    """Extract and clamp the row's case_stem field."""
    return _cell_to_str(row.get('case_stem')).strip()[:CASE_STEM_MAX_LENGTH]


def _parse_optional_positive_int(value):
    text = _cell_to_str(value).strip()
    if not text:
        return None
    try:
        numeric = float(text)
        parsed = int(numeric) if numeric.is_integer() else None
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None or parsed < 1:
        raise ValueError('ترتيب السؤال في الحالة غير صالح')
    return parsed


def _parse_optional_date(value):
    text = _cell_to_str(value).strip()
    if not text:
        return None
    parsed = pd.to_datetime(text, errors='coerce')
    if pd.isna(parsed):
        raise ValueError('تاريخ آخر مراجعة غير صالح')
    return parsed.date()


def _extract_tags(row):
    """Read lossless tag arrays/JSON before the legacy comma column."""
    for key in ('tags', 'tag_names'):
        value = row.get(key)
        if isinstance(value, list):
            if any(not isinstance(tag, str) for tag in value):
                raise ValueError('يجب أن تكون جميع الوسوم نصوصاً')
            return [clean_tag_name(str(tag)) for tag in value if str(tag).strip()]

    raw_json = _cell_to_str(row.get('tags_json')).strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            if any(not isinstance(tag, str) for tag in parsed):
                raise ValueError('يجب أن تكون جميع الوسوم نصوصاً')
            return [clean_tag_name(str(tag)) for tag in parsed if str(tag).strip()]

    return [
        clean_tag_name(tag)
        for tag in _cell_to_str(row.get('tags', '')).split(',')
        if tag.strip()
    ]


def _portable_question_uuid(row):
    raw = _cell_to_str(row.get('uuid')).strip()
    if not raw:
        return None
    try:
        return uuid_module.UUID(raw)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('uuid السؤال غير صالح')


def _resolve_knowledge_object(row, author=None):
    nested = (
        row.get('knowledge_object')
        if isinstance(row.get('knowledge_object'), dict) else {}
    )
    raw_uuid = (
        _cell_to_str(row.get('knowledge_object_uuid')).strip()
        or _cell_to_str(nested.get('uuid')).strip()
    )
    title = (
        _cell_to_str(row.get('knowledge_object_title')).strip()
        or _cell_to_str(nested.get('title')).strip()
    )[:200]
    objective = (
        _cell_to_str(row.get('learning_objective')).strip()
        or _cell_to_str(nested.get('learning_objective')).strip()
        or title
    )[:QUESTION_TEXT_MAX_LENGTH]
    if not raw_uuid and not title:
        return None

    object_uuid = None
    if raw_uuid:
        try:
            object_uuid = uuid_module.UUID(raw_uuid)
        except (ValueError, TypeError, AttributeError):
            raise ValueError('uuid الهدف المعرفي غير صالح')
        existing = KnowledgeObject.objects.filter(uuid=object_uuid).first()
        if existing is not None:
            return existing
    if title:
        existing = KnowledgeObject.objects.filter(title=title).first()
        if existing is not None:
            return existing
    if not title:
        return None
    kwargs = {
        'title': title,
        'learning_objective': objective,
        'created_by': author,
    }
    if object_uuid:
        kwargs['uuid'] = object_uuid
    return KnowledgeObject.objects.create(**kwargs)


def _extract_translations(row):
    raw = row.get('translations')
    if not isinstance(raw, dict):
        raw = row.get('translations_json', raw)
    if raw is None or (isinstance(raw, float) and pd.isna(raw)) or raw == '':
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError('translations_json غير صالح') from None
    return normalize_translations(raw, max_choices=MAX_CHOICES)


# ── Question construction ──────────────────────────────────────────────

def _build_question(row, username, author=None, owner=None):
    """
    Build (but do not save) a Question from one row.

    Raises ValueError with a user-facing Arabic message on any
    validation failure. The caller (`_persist_records`) lets that
    propagate; `import_file` catches it and returns a 400.

    FAILURE ORDER
    -------------
    Choice-list validation runs before correct-answer parsing, matching
    the pre-refactor order. A row with both a malformed choice list
    and an unparseable `correct_answer` cell reports the choice error
    first, exactly as it did before the validation was extracted into
    `apps.questions.validation`.

    `clean_and_validate_choices` is called with `correct_answer=None`
    on the first pass so that only the choice-list rules run there —
    the correct-answer range check is done below via the shared
    `validate_correct_answer` helper, so that the message stays
    identical to the one every other write path produces.

    SINGLE SOURCE OF TRUTH (fix — validation.py owns the message)
    -------------------------------------------------------------
    The correct-answer range check used to be an inline comparison
    with a hand-written `f'رقم الإجابة الصحيحة يجب أن يكون بين 1 و
    {len(cleaned_choices)}'` string. That string was the same one
    `validation.clean_and_validate_choices` already produced, but
    because this module needed the "unparseable integer" and
    "out of range" errors to stay distinct (and in that order), the
    reuse was not made. The fix: parse the cell first (keeping the
    distinct parse error), then call the shared `validate_correct_
    answer` helper, which now owns both the rule and the message.
    """
    if owner is None:
        owner = get_user_safe(username)
    if author is None:
        author = owner

    q_text = _cell_to_str(row.get('question', '')).strip()
    if not q_text:
        raise ValueError('نص السؤال لا يمكن أن يكون فارغاً')

    if len(q_text) > QUESTION_TEXT_MAX_LENGTH:
        raise ValueError(
            f'نص السؤال يتجاوز الحد الأقصى ({QUESTION_TEXT_MAX_LENGTH} حرفاً). '
            f'الطول الحالي: {len(q_text)}'
        )

    choices = _extract_choices(row)

    # Step 1 — choice-list validation. `correct_answer=None` tells the
    # shared helper to skip the range check; that runs in step 3.
    cleaned_choices, choice_error = clean_and_validate_choices(
        choices,
        None,
        max_choices=MAX_CHOICES,
        allow_duplicates=True,
    )
    if choice_error is not None:
        raise ValueError(choice_error['message'])

    # Step 2 — parse the correct-answer cell.
    raw_correct = row.get('correct_answer', 1)
    try:
        if isinstance(raw_correct, bool):
            raise ValueError
        numeric_correct = float(raw_correct)
        if not numeric_correct.is_integer():
            raise ValueError
        correct = int(numeric_correct)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('رقم الإجابة الصحيحة غير صالح')

    # Step 3 — range check via the shared helper.
    range_error = validate_correct_answer(correct, len(cleaned_choices))
    if range_error is not None:
        raise ValueError(range_error['message'])

    tags = _extract_tags(row)

    case = _resolve_case_from_row(row, author=author)
    knowledge_object = _resolve_knowledge_object(row, author=author)

    explanation = _cell_to_str(row.get('explanation'))
    if len(explanation) > EXPLANATION_TEXT_MAX_LENGTH:
        raise ValueError(
            f'الشرح يتجاوز الحد الأقصى ({EXPLANATION_TEXT_MAX_LENGTH} حرفاً)'
        )

    question_kwargs = dict(
        question=q_text,
        choices=cleaned_choices,
        correct_answer=correct,
        explanation=explanation,
        source=_cell_to_str(row.get('source'))[:200],
        source_document=_cell_to_str(row.get('source_document'))[:500] or None,
        source_page=_parse_optional_positive_int(row.get('source_page')),
        translations=_extract_translations(row),
        difficulty=parse_difficulty(row.get('difficulty', 'medium')),
        category_id=_resolve_category(row),
        knowledge_object=knowledge_object,
        authored_by=author,
        owned_by=owner,
        verified=False,
        case=case,
        case_order=_parse_optional_positive_int(row.get('case_order')),
    )
    portable_uuid = _portable_question_uuid(row)
    if portable_uuid is not None:
        question_kwargs['uuid'] = portable_uuid
    revised_at = _parse_optional_date(row.get('last_revised_at'))
    if revised_at is not None:
        question_kwargs['last_revised_at'] = revised_at
    question = Question(**question_kwargs)
    return question, tags


def _persist_records(records, username, author=None, owner=None):
    """
    Persist a list of already-extracted record dicts.

    All rows go into one transaction, so a mid-list validation failure
    rolls back the whole batch rather than committing a partial
    import. Returns the number of rows written.

    Called by flat_import.import_file and telegram_import.import_telegram.
    """
    if owner is None:
        owner = get_user_safe(username)
    if author is None:
        author = owner

    count = 0
    skipped = 0
    flagged = 0
    seen_question_keys = {
        quality_key(text)
        for text in Question.objects.values_list('question', flat=True)
    }
    with transaction.atomic():
        for row in records:
            question, tags = _build_question(
                row, username, author=author, owner=owner,
            )
            if Question.objects.filter(uuid=question.uuid).exists():
                skipped += 1
                continue
            (
                question.question,
                question.choices,
                quality_issues,
            ) = prepare_import_content(
                question.question,
                question.choices,
                seen_question_keys,
            )
            question.save()
            for tag_name in tags:
                tag, _ = Tag.objects.get_or_create(name=tag_name)
                question.tags.add(tag)
            if flag_import_quality_issues(question, owner, quality_issues):
                flagged += 1
            count += 1
    return count, skipped, flagged


# ── Public entry point ─────────────────────────────────────────────────

def import_file(file, username):
    """
    Import a flat XLSX / XLS / CSV / JSON file.

    The uploaded file is written to a temp file under
    settings.UPLOAD_FOLDER and removed in the finally block.

    TEMP-FILE NAME
    --------------
    Files are named `import_<uuid>.<ext>`. The `import_` prefix is
    load-bearing: `apps/core/utils.py::cleanup_old_temp_files` sweeps
    UPLOAD_FOLDER on a schedule and only removes files matching the
    prefixes `import_`, `questions_export_`, `verified_questions_export_`,
    or `pre_restore_`. Before this prefix was added, an upload that
    was orphaned mid-import (process killed, OOM, request cancelled)
    was never swept — it sat in UPLOAD_FOLDER forever.
    """
    ext = file.name.rsplit('.', 1)[1].lower() if '.' in file.name else ''
    if ext not in ['xlsx', 'xls', 'csv', 'json']:
        return {'error': 'نوع الملف غير مسموح', 'code': 400}

    mime_error = verify_upload_mime(file)
    if mime_error is not None:
        return mime_error

    user = get_user_safe(username)
    if not user:
        return {'error': 'المستخدم غير موجود', 'code': 404}

    filepath = stage_upload(file, Path(file.name).suffix)

    try:
        try:
            if ext == 'xlsx':
                df = pd.read_excel(filepath, engine='openpyxl')
            elif ext == 'xls':
                df = pd.read_excel(filepath, engine='xlrd')
            elif ext == 'csv':
                df = pd.read_csv(filepath, encoding='utf-8-sig')
            elif ext == 'json':
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not isinstance(data, (list, dict)):
                    return {'error': 'يجب أن يحتوي ملف JSON على كائن أو قائمة كائنات', 'code': 400}
                items = data if isinstance(data, list) else [data]
                if len(items) > MAX_JSON_IMPORT_ELEMENTS:
                    return {
                        'error': f'ملف JSON كبير جداً. الحد الأقصى {MAX_JSON_IMPORT_ELEMENTS} عنصر.',
                        'code': 400,
                    }
                if any(not isinstance(item, dict) for item in items):
                    return {'error': 'يجب أن تكون عناصر JSON كائنات', 'code': 400}
                df = pd.DataFrame(items)
            else:
                return {'error': 'صيغة الملف غير مدعومة', 'code': 400}
        except Exception as exc:
            logger.warning('Unable to parse import file %r: %s', file.name, exc)
            return {'error': 'تعذر قراءة الملف أو أن تنسيقه غير صالح', 'code': 400}

        if ext == 'json':
            if 'choices' not in df.columns:
                return {'error': "عمود choices مطلوب في ملف JSON", 'code': 400}
        else:
            choice_cols = [c for c in df.columns if c.startswith('choice_')]
            if len(choice_cols) < 2:
                return {
                    'error': "يجب توفير عمودين على الأقل من الاختيارات (choice_1, choice_2, ...)",
                    'code': 400,
                }

        if df.empty:
            return {'error': 'الملف لا يحتوي على بيانات', 'code': 400}

        records = df.to_dict(orient='records')
        if len(records) > settings.MAX_IMPORT_QUESTIONS:
            return {
                'error': (
                    f'عدد الأسئلة ({len(records)}) يتجاوز '
                    f'الحد ({settings.MAX_IMPORT_QUESTIONS}). '
                    'قسّم الملف بدلاً من استيراد جزء منه بصمت.'
                ),
                'code': 400,
            }

        count, skipped, flagged = _persist_records(
            records, username, author=user, owner=user,
        )

        user.update_trust_score()
        return {
            'message': f'تم استيراد {count} سؤال بنجاح',
            'imported': count,
            'skipped': skipped,
            'quality_flags_created': flagged,
        }

    except ValueError as ve:
        return {'error': str(ve), 'code': 400}
    except Exception as e:
        logger.exception('Import failed: %s', e)
        return {'error': 'فشل استيراد الملف', 'code': 500}
    finally:
        cleanup_staged_upload(filepath)
