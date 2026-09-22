# backend/apps/questions/services/importing/flat_import.py
"""
Flat import path: XLSX / XLS / CSV / JSON lists of questions.

Each row carries the question text, a `choice_1..choice_N` set of
columns (or, for JSON, a `choices` list), a correct_answer index, and
optional metadata (category, tags, difficulty, case_group/case_stem).

This module is the original `ImportService.import_file` and its
private helpers, unchanged except for the mechanical move from
class-static to module-level functions.
"""

import hashlib
import json
import logging
from pathlib import Path

import pandas as pd

from django.conf import settings
from django.db import transaction

from ...models import (
    Question,
    Category,
    Tag,
    ClinicalCase,
    clean_tag_name,
    QUESTION_TEXT_MAX_LENGTH,
    CHOICE_TEXT_MAX_LENGTH,
    CASE_STEM_MAX_LENGTH,
    CASE_GROUP_MAX_LENGTH,
)
from ...validation import (
    clean_and_validate_choices,
    validate_correct_answer,
)

from .validators import (
    MAX_CHOICES,
    MAX_JSON_IMPORT_ELEMENTS,
    _cell_to_str,
    get_user_safe,
    parse_difficulty,
    verify_upload_mime,
)
from .staging import stage_upload, cleanup_staged_upload

logger = logging.getLogger(__name__)


# ── Row-level helpers ──────────────────────────────────────────────────

def _resolve_category(row):
    """
    Resolve a row's category reference to a Category.id, or None.

    Prefers an explicit `category_id`; falls back to a `category` or
    `category_name` lookup. A miss is not an error — the question is
    imported with category=None, which is a legal state.
    """
    category_id_raw = _cell_to_str(row.get('category_id')).strip()
    if category_id_raw:
        try:
            return int(category_id_raw)
        except (ValueError, TypeError):
            pass

    category_name = (
        _cell_to_str(row.get('category')).strip()
        or _cell_to_str(row.get('category_name')).strip()
    )
    if category_name:
        try:
            category = Category.objects.get(name=category_name)
            return category.id
        except Category.DoesNotExist:
            pass

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
        return row['choices']
    if 'choices' in row and isinstance(row['choices'], str):
        try:
            parsed = json.loads(row['choices'])
            if isinstance(parsed, list):
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
    case_key = (
        _cell_to_str(row.get('case_key')).strip()
        or _cell_to_str(row.get('case_group')).strip()
    )[:CASE_GROUP_MAX_LENGTH]

    case_stem = _cell_to_text_stem(row)

    if not case_key and not case_stem:
        return None

    if not case_key:
        digest = hashlib.sha256(case_stem.encode('utf-8')).hexdigest()[:12]
        case_key = f'auto-{digest}'

    case, created = ClinicalCase.objects.get_or_create(
        key=case_key,
        defaults={
            'stem': case_stem or None,
            'authored_by': author,
        },
    )

    if not created and case_stem and not case.stem:
        case.stem = case_stem
        case.save(update_fields=['stem', 'updated_at'])

    return case


def _cell_to_text_stem(row):
    """Extract and clamp the row's case_stem field."""
    return _cell_to_str(row.get('case_stem')).strip()[:CASE_STEM_MAX_LENGTH]


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

    q_text = str(row.get('question', '')).strip()
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
    )
    if choice_error is not None:
        raise ValueError(choice_error['message'])

    # Step 2 — parse the correct-answer cell.
    raw_correct = row.get('correct_answer', 1)
    try:
        correct = int(raw_correct)
    except (TypeError, ValueError):
        raise ValueError('رقم الإجابة الصحيحة غير صالح')

    # Step 3 — range check via the shared helper.
    range_error = validate_correct_answer(correct, len(cleaned_choices))
    if range_error is not None:
        raise ValueError(range_error['message'])

    tags = [
        clean_tag_name(t)
        for t in _cell_to_str(row.get('tags', '')).split(',')
        if t.strip()
    ]

    case = _resolve_case_from_row(row, author=author)

    question = Question(
        question=q_text,
        choices=cleaned_choices,
        correct_answer=correct,
        explanation=_cell_to_str(row.get('explanation')),
        source=_cell_to_str(row.get('source'))[:200],
        difficulty=parse_difficulty(row.get('difficulty', 'medium')),
        category_id=_resolve_category(row),
        authored_by=author,
        owned_by=owner,
        verified=False,
        case=case,
    )
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
    with transaction.atomic():
        for row in records:
            question, tags = _build_question(
                row, username, author=author, owner=owner,
            )
            question.save()
            for tag_name in tags:
                tag, _ = Tag.objects.get_or_create(name=tag_name)
                question.tags.add(tag)
            count += 1
    return count


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
        if ext == 'xlsx':
            df = pd.read_excel(filepath, engine='openpyxl')
        elif ext == 'xls':
            df = pd.read_excel(filepath, engine='xlrd')
        elif ext == 'csv':
            df = pd.read_csv(filepath, encoding='utf-8-sig')
        elif ext == 'json':
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list) and len(data) > MAX_JSON_IMPORT_ELEMENTS:
                return {
                    'error': f'ملف JSON كبير جداً. الحد الأقصى {MAX_JSON_IMPORT_ELEMENTS} عنصر.',
                    'code': 400,
                }
            df = pd.DataFrame(data if isinstance(data, list) else [data])
        else:
            return {'error': 'صيغة الملف غير مدعومة', 'code': 400}

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
            records = records[:settings.MAX_IMPORT_QUESTIONS]

        count = _persist_records(
            records, username, author=user, owner=user,
        )

        user.update_trust_score()
        return {'message': f'تم استيراد {count} سؤال بنجاح'}

    except ValueError as ve:
        return {'error': str(ve), 'code': 400}
    except Exception as e:
        logger.exception('Import failed: %s', e)
        return {'error': 'فشل استيراد الملف', 'code': 500}
    finally:
        cleanup_staged_upload(filepath)