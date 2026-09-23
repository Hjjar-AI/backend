# backend/apps/questions/services/exporting/flat_export.py
"""
Flat export path: XLSX / CSV / JSON / PDF lists of questions with
choice_1..choice_N columns (or a `choices` list for JSON).

PDF is dispatched to the sibling `pdf_export` module. This file builds
the filtered queryset ONCE and passes the materialized list to
pdf_export, so the two paths cannot drift in which rows they select.

FILTER SUPPORT
--------------
`export_questions` accepts an optional `filters` dict. Every key is
optional; an empty dict (or None) means "no filters" and produces the
same output as before this feature existed. Supported keys:

    search        — substring match on question / explanation / source
    difficulty    — comma-separated list of 'easy' | 'medium' | 'hard'
    category_ids  — comma-separated ints, or a list of ints
    tag           — single tag name
    tags_filter   — comma-separated tag names, or a list of tag names

TITLE SUPPORT
-------------
`export_questions` also accepts an optional `title`. It is used by
PDF export only.

FORMAT-FIRST BRANCHING (fix — wasted DataFrame build on JSON)
-------------------------------------------------------------
The previous revision built the Excel/CSV row list and the
`pandas.DataFrame` UNCONDITIONALLY, before the format was even
checked, then for JSON discarded the DataFrame and iterated the
queryset a second time to build `json_data` with its own hand-copied
field mapping. Every JSON export therefore paid for:

  • A full Python loop over every question building the Excel row
    dict.
  • A `pandas.DataFrame` construction that was thrown away.
  • A second Python loop re-reading ~20 of the same fields from the
    same objects.

The DB cost was not doubled (Django caches the queryset after the
first iteration), but the CPU and memory cost was. The function now
branches on `fmt` FIRST and builds only the structure the caller
actually asked for. The `export_dir` / `prefix` computation follows
the PDF dispatch, so the PDF path does not pay for a directory
mkdir it does not use (matching the pre-refactor behaviour).
"""

import json
import logging
from pathlib import Path

import pandas as pd

from django.conf import settings
from apps.core.artifacts import reserve_artifact_path, download_filename

from ...models import Question
from ...filters import filter_questions

from .formula_sanitizer import sanitize_formula_cell
from .pdf_export import export_questions_pdf

logger = logging.getLogger(__name__)


def _build_export_queryset(filters, verified_only):
    """Build the same filtered, prefetched queryset for every export format."""
    queryset = (
        Question.objects
        .select_related('case', 'category', 'authored_by', 'owned_by')
        .prefetch_related('tags')
        .order_by('id')
    )
    return filter_questions(queryset, filters, export=True, verified_only=verified_only)


# ── Row builders ──────────────────────────────────────────────────────
#
# The Excel/CSV row shape and the JSON item shape overlap on ~20
# fields but differ in two ways:
#
#   • Excel/CSV flattens choices into `choice_1..choice_N` columns
#     so a spreadsheet consumer sees one column per position.
#   • JSON nests the choices in a `choices` list and groups case
#     fields under a single `case` object.
#
# The two were hand-built in the same function, field for field, so
# a new question field had to be added to both branches or it
# silently appeared in only one format. Each format now has its own
# builder function; a future field addition is a one-line change
# per format, and the compiler (via the shared field list) makes it
# obvious that both need updating.


def _row_for_flat_format(q, max_choices):
    """Row dict for Excel and CSV exports."""
    case_key = q.case.key if q.case_id else ''
    case_stem = (q.case.stem if q.case_id else None) or ''

    authored_by_username = q.authored_by.username if q.authored_by_id else ''
    owned_by_username = q.owned_by.username if q.owned_by_id else ''

    tag_names = [t.name for t in q.tags.all()]
    row = {
        'id': q.id,
        'uuid': str(q.uuid),
        'question': sanitize_formula_cell(q.question),
        'correct_answer': q.correct_answer,
        'explanation': sanitize_formula_cell(q.explanation),
        'source': sanitize_formula_cell(q.source),
        # Keep the legacy comma-separated column for spreadsheet users and add
        # a lossless JSON column for names that themselves contain commas.
        'tags': sanitize_formula_cell(','.join(tag_names)),
        'tags_json': json.dumps(tag_names, ensure_ascii=False),
        'difficulty': q.difficulty,
        'category_id': q.category_id,
        'category_uuid': str(q.category.uuid) if q.category_id else None,
        'category_name': sanitize_formula_cell(q.category.name) if q.category_id else '',
        'verified': q.verified,
        'verified_by': sanitize_formula_cell(q.verified_by),
        'verified_at': q.verified_at.isoformat() if q.verified_at else None,
        'verification_notes': sanitize_formula_cell(q.verification_notes),
        'authored_by': sanitize_formula_cell(authored_by_username),
        'authored_by_id': q.authored_by_id,
        'authored_by_uuid': str(q.authored_by.uuid) if q.authored_by_id else None,
        'owned_by': sanitize_formula_cell(owned_by_username),
        'owned_by_id': q.owned_by_id,
        'owned_by_uuid': str(q.owned_by.uuid) if q.owned_by_id else None,
        'created_at': q.created_at.isoformat() if q.created_at else None,
        'updated_at': q.updated_at.isoformat() if q.updated_at else None,
        'updated_by': sanitize_formula_cell(q.updated_by),
        'times_answered': q.times_answered,
        'times_correct': q.times_correct,
        'case_key': case_key,
        'case_group': case_key,   # legacy column name, same value
        'case_uuid': str(q.case.uuid) if q.case_id else None,
        'case_title': sanitize_formula_cell(q.case.title) if q.case_id else '',
        'case_stem': sanitize_formula_cell(case_stem),
        'case_order': q.case_order,
    }
    choices = q.choices if isinstance(q.choices, list) else []
    for i in range(1, max_choices + 1):
        cell = choices[i - 1] if i <= len(choices) else ''
        row[f'choice_{i}'] = sanitize_formula_cell(cell)
    return row


def _row_for_json(q):
    """Item dict for JSON export."""
    tag_names = [t.name for t in q.tags.all()]
    return {
        'id': q.id,
        'uuid': str(q.uuid),
        # JSON is a data format, not a spreadsheet.  Formula-prefix escaping
        # here used to corrupt legitimate leading '=', '+', '-', and '@'
        # characters on a JSON export/import round trip.
        'question': q.question,
        'choices': list(q.choices) if isinstance(q.choices, list) else [],
        'correct_answer': q.correct_answer,
        'explanation': q.explanation,
        'source': q.source,
        # Preserve the long-standing comma-separated field for existing API
        # consumers, while ``tag_names`` provides a lossless representation
        # for names that contain commas.
        'tags': ','.join(tag_names),
        'tag_names': tag_names,
        'difficulty': q.difficulty,
        'category_id': q.category_id,
        'category_uuid': str(q.category.uuid) if q.category_id else None,
        'category_name': q.category.name if q.category_id else None,
        'verified': q.verified,
        'verified_by': q.verified_by,
        'verified_at': q.verified_at.isoformat() if q.verified_at else None,
        'verification_notes': q.verification_notes,
        'authored_by': q.authored_by.username if q.authored_by_id else None,
        'authored_by_id': q.authored_by_id,
        'authored_by_uuid': (
            str(q.authored_by.uuid) if q.authored_by_id else None
        ),
        'owned_by': q.owned_by.username if q.owned_by_id else None,
        'owned_by_id': q.owned_by_id,
        'owned_by_uuid': (
            str(q.owned_by.uuid) if q.owned_by_id else None
        ),
        'created_at': q.created_at.isoformat() if q.created_at else None,
        'updated_at': q.updated_at.isoformat() if q.updated_at else None,
        'updated_by': q.updated_by,
        'times_answered': q.times_answered,
        'times_correct': q.times_correct,
        'case': (
            {
                'uuid': str(q.case.uuid),
                'key': q.case.key,
                'title': q.case.title,
                'stem': q.case.stem,
            }
            if q.case_id
            else None
        ),
        'case_order': q.case_order,
    }


# ── Public entry point ─────────────────────────────────────────────

def export_questions(
    fmt='excel', verified_only=False, filters=None, title=None, theme=None,
    locale='ar', front_matter=None,
):
    """
    Export the filtered question bank as XLSX, CSV, JSON, or PDF.

    `title` is a document label used by PDF export only. Excel, CSV,
    and JSON ignore it — see the module docstring for why. When
    `fmt='pdf'` and `title` is provided, it becomes the header banner
    text and the descriptive segment of the generated filename.

    `theme` is also PDF-only. It names the browser's active palette;
    the PDF renderer validates it and falls back to Stone when absent
    or unknown.

    Returns {'filepath', 'filename'} on success, or
    {'error', 'code'} on failure (no data after filtering, unknown
    format, PDF engine missing).
    """
    queryset = _build_export_queryset(filters, verified_only)
    if not queryset.exists():
        return {'error': 'لا توجد بيانات للتصدير', 'code': 404}

    # ── PDF — dispatched to the sibling module ────────────────────
    #
    # Placed FIRST, before the export_dir/prefix computation, so the
    # PDF path does not pay for a mkdir that pdf_export itself will
    # perform later. This matches the pre-refactor ordering.
    #
    # Materializing the queryset here guarantees the PDF contains
    # exactly the same rows the other formats would have contained
    # for the same filter set.
    if fmt == 'pdf':
        return export_questions_pdf(
            list(queryset),
            verified_only=verified_only,
            filters=filters,
            title=title,
            theme=theme,
            locale=locale,
            front_matter=front_matter,
        )

    export_dir = Path(settings.EXPORT_FOLDER)
    export_dir.mkdir(parents=True, exist_ok=True)
    prefix = 'verified_questions_export' if verified_only else 'questions_export'

    # ── JSON — build only the JSON item list ──────────────────────
    if fmt == 'json':
        json_data = [_row_for_json(q) for q in queryset]
        filepath = reserve_artifact_path(export_dir, prefix, '.json')
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2, default=str)
        return {'filepath': str(filepath), 'filename': download_filename(filepath)}

    # ── Excel / CSV — build the flat row list once ────────────────
    if fmt in ('excel', 'csv'):
        max_choices = getattr(settings, 'MAX_CHOICES', 8)
        data = [_row_for_flat_format(q, max_choices) for q in queryset]
        df = pd.DataFrame(data)

        if fmt == 'excel':
            filepath = reserve_artifact_path(export_dir, prefix, '.xlsx')
            df.to_excel(filepath, index=False, engine='openpyxl')
        else:  # csv
            filepath = reserve_artifact_path(export_dir, prefix, '.csv')
            df.to_csv(filepath, index=False, encoding='utf-8-sig')
        return {'filepath': str(filepath), 'filename': download_filename(filepath)}

    # ── Unknown format ────────────────────────────────────────────
    return {'error': 'صيغة التصدير غير صالحة', 'code': 400}
