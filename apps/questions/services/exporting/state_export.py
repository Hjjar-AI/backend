# backend/apps/questions/services/exporting/state_export.py
"""
State-envelope export path (format v2).

Produces the full questions-data envelope: categories, tags, cases,
questions, and a user_map for the mapping UI. Images are embedded as
base64 when requested.
"""

import json
import logging
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from apps.core.artifacts import reserve_artifact_path, download_filename
from apps.questions.services.state_format import STATE_FORMAT, STATE_FORMAT_VERSION
from apps.questions.services.state_workbook import write_state_workbook

from ...models import Question, Category, Tag, ClinicalCase

from .image_export import read_image_as_base64

logger = logging.getLogger(__name__)


# Version and format identifiers for the state envelope.
#
# v2 — emitted by this revision. Adds:
#   • uuid on every ported entity (categories, tags, cases, questions)
#   • authored_by_uuid / owned_by_uuid per question
#   • meta.user_map: uuid → { username, full_name } for every user
#     referenced by any question OR case in the envelope, so a
#     same-user-table round trip resolves authors without additional
#     lookups, and a mapping UI has a name for every referenced uuid.
#   • is_draft per question, and a draft_created/draft_skipped pair
#     in the import result counts. Prior to this field the importer
#     silently published every draft the envelope carried, because
#     `Question.objects.create(...)` fell through to the model
#     default `is_draft=False`. The field is additive: an importer
#     that ignores it stays buggy but does not error.
#
# Only v2 is accepted by the importer (see
# state_import._validate_state_envelope). No v1 data exists yet.


def export_state(include_images=True, verified_only=False, fmt='json'):
    """
    Build the full questions-data envelope (format v2).

    Sections:
      • categories — full field set, keyed by uuid.
      • tags       — name + parent uuid (nullable), keyed by uuid.
      • cases      — uuid + key + title + stem + authored_by_uuid.
      • questions  — complete portable question data, including
                     content, relationships, moderation state,
                     statistics, optimistic-lock version, and audit
                     timestamps.
      • meta.user_map — { user_uuid: {username, full_name} } for
                     every user referenced by ANY ported entity
                     (question authored_by/owned_by, or case
                     authored_by). Informational only — the
                     importer matches by uuid, not by name.

    ``fmt`` may be ``json`` (the canonical envelope) or ``xlsx`` (a
    lossless multi-sheet container around that same envelope).
    Images are embedded as base64. `include_images=False` produces
    a much smaller file and writes no image data.

    `verified_only=True` restricts questions to those already
    verified. Categories, tags, and cases referenced by at least
    one exported question are all included; orphans (a case with
    no exported question, a category with no exported question)
    are dropped because nothing in the envelope would reference
    them.

    DRAFTS ARE INCLUDED. The query deliberately uses
    `Question.objects` (default manager, i.e. `.all()`) rather
    than `.public()`. The state envelope is a full backup; a
    backup that silently omitted drafts would let the bug that
    this field fixes recur through the export side instead.
    """
    fmt = str(fmt or 'json').lower().strip()
    if fmt not in {'json', 'xlsx'}:
        return {'error': 'صيغة تصدير الحالة غير مدعومة', 'code': 400}

    payload = {
        'meta': {
            'format': STATE_FORMAT,
            'version': STATE_FORMAT_VERSION,
            'exported_at': timezone.now().isoformat(),
            'includes_images': bool(include_images),
            'user_map': {},
            'counts': {},
        },
        'categories': [],
        'tags': [],
        'cases': [],
        'questions': [],
    }

    # ── Questions ─────────────────────────────────────────────────
    qs = (
        Question.objects
        .select_related('case', 'category', 'authored_by', 'owned_by')
        .prefetch_related('tags')
    )
    if verified_only:
        qs = qs.filter(verified=True)

    question_count = qs.count()
    question_limit = settings.MAX_STATE_IMPORT_QUESTIONS
    if question_count > question_limit:
        return {
            'error': (
                f'لا يمكن إنشاء حزمة قابلة للاستيراد: '
                f'عدد الأسئلة ({question_count}) يتجاوز الحد '
                f'({question_limit}).'
            ),
            'code': 413,
        }

    used_category_ids = set()
    used_tag_ids = set()
    used_case_ids = set()
    used_user_ids = set()

    for q in qs:
        if q.category_id:
            used_category_ids.add(q.category_id)
        if q.case_id:
            used_case_ids.add(q.case_id)
        if q.authored_by_id:
            used_user_ids.add(q.authored_by_id)
        if q.owned_by_id:
            used_user_ids.add(q.owned_by_id)

        image_block = None
        if include_images and q.image:
            image_block = read_image_as_base64(q.image)

        question_payload = {
            'uuid': str(q.uuid),
            'question': q.question,
            'choices': list(q.choices or []),
            'correct_answer': q.correct_answer,
            'explanation': q.explanation or '',
            'source': q.source or '',
            'difficulty': q.difficulty,
            'category_uuid': (
                str(q.category.uuid) if q.category_id else None
            ),
            'tags': [],
            'case_uuid': str(q.case.uuid) if q.case_id else None,
            'case_order': q.case_order,
            # Draft state is portable content state, not per-user
            # private state. It travels with the question; the
            # importer decides what owner to assign (see
            # state_import/apply.py — draft ownership always
            # transfers to the importing admin, because the
            # original draft_owner may not exist on the target).
            'is_draft': bool(q.is_draft),
            'verified': q.verified,
            'verified_by': q.verified_by or None,
            'verified_at': q.verified_at.isoformat() if q.verified_at else None,
            'verification_notes': q.verification_notes or None,
            # Authorship. `authored_by_uuid` is the identity the
            # importing side maps against its own user table.
            # `authored_by_name` is informational — the mapping UI
            # uses it to label the prompt. `owned_by_uuid` and
            # `owned_by_name` are included for completeness; the
            # importer OVERRIDES ownership to the acting admin on
            # every import, per the design decision (ownership is
            # local by definition — whoever imports is the steward).
            'authored_by_uuid': (
                str(q.authored_by.uuid) if q.authored_by_id else None
            ),
            'authored_by_name': (
                q.authored_by.username if q.authored_by_id else None
            ),
            'owned_by_uuid': (
                str(q.owned_by.uuid) if q.owned_by_id else None
            ),
            'owned_by_name': (
                q.owned_by.username if q.owned_by_id else None
            ),
            'updated_by': q.updated_by or None,
            'times_answered': q.times_answered,
            'times_correct': q.times_correct,
            'version': q.version,
            'created_at': q.created_at.isoformat() if q.created_at else None,
            'updated_at': q.updated_at.isoformat() if q.updated_at else None,
            'image': image_block,
        }
        tag_list = list(q.tags.all())
        for tag in tag_list:
            used_tag_ids.add(tag.id)
        question_payload['tags'] = [str(tag.uuid) for tag in tag_list]

        payload['questions'].append(question_payload)

    # ── Cases ─────────────────────────────────────────────────────
    #
    # NOTE: case authors are added to `used_user_ids` here so they
    # appear in `meta.user_map` below. Without this line, a case
    # whose author does not currently author or own any exported
    # question would be referenced by uuid in `cases[]` but have
    # no entry in the map — the mapping UI would have nothing to
    # display for it.
    for case in ClinicalCase.objects.filter(id__in=used_case_ids):
        if case.authored_by_id:
            used_user_ids.add(case.authored_by_id)
        payload['cases'].append({
            'uuid': str(case.uuid),
            'key': case.key,
            'title': case.title or '',
            'stem': case.stem or '',
            'authored_by_uuid': (
                str(case.authored_by.uuid) if case.authored_by_id else None
            ),
            'authored_by_name': (
                case.authored_by.username if case.authored_by_id else None
            ),
        })

    # ── Categories ────────────────────────────────────────────────
    for cat in Category.objects.filter(id__in=used_category_ids):
        payload['categories'].append({
            'uuid': str(cat.uuid),
            'name': cat.name,
            'description': cat.description or '',
            'color': cat.color,
            'icon': cat.icon,
        })

    # ── Tags ──────────────────────────────────────────────────────
    # Include the complete ancestor chain for every assigned tag.
    # A child without its unassigned parent would still import, but
    # the hierarchy would silently flatten during a round trip.
    all_tag_by_id = {
        tag.id: tag
        for tag in Tag.objects.select_related('parent')
    } if used_tag_ids else {}
    expanded_tag_ids = set(used_tag_ids)
    for tag_id in tuple(used_tag_ids):
        current = all_tag_by_id.get(tag_id)
        visited = set()
        while current and current.parent_id and current.parent_id not in visited:
            visited.add(current.parent_id)
            expanded_tag_ids.add(current.parent_id)
            current = all_tag_by_id.get(current.parent_id)
    tag_by_id = {
        tag_id: all_tag_by_id[tag_id]
        for tag_id in expanded_tag_ids
        if tag_id in all_tag_by_id
    }
    for tag in tag_by_id.values():
        parent_uuid = None
        if tag.parent_id and tag.parent_id in tag_by_id:
            parent_uuid = str(tag_by_id[tag.parent_id].uuid)
        payload['tags'].append({
            'uuid': str(tag.uuid),
            'name': tag.name,
            'parent_uuid': parent_uuid,
        })

    # ── user_map ──────────────────────────────────────────────────
    # Informational only. Gives the importer a human-readable
    # name for each referenced uuid without a second query. It
    # does NOT participate in resolution — the importer matches
    # by uuid, not by name.
    if used_user_ids:
        from apps.users.models import User
        for u in User.objects.filter(id__in=used_user_ids).only(
            'id', 'uuid', 'username', 'full_name',
        ):
            payload['meta']['user_map'][str(u.uuid)] = {
                'username': u.username,
                'full_name': u.full_name or '',
            }

    payload['meta']['counts'] = {
        'categories': len(payload['categories']),
        'tags': len(payload['tags']),
        'cases': len(payload['cases']),
        'questions': len(payload['questions']),
        'users': len(payload['meta']['user_map']),
    }

    transfer_limit = min(
        settings.MAX_STATE_TRANSFER_SIZE,
        settings.MAX_UPLOAD_SIZE,
    )
    if fmt == 'xlsx':
        # XLSX is compressed. Bound the canonical, reconstructed state too,
        # otherwise a highly-compressible workbook could be downloadable but
        # too large for the state importer once decoded.
        canonical_size = len(json.dumps(
            payload,
            ensure_ascii=False,
            separators=(',', ':'),
            default=str,
        ).encode('utf-8'))
        if canonical_size > transfer_limit:
            return {
                'error': (
                    'حجم حزمة الحالة يتجاوز حد الاستيراد. '
                    'صدّر الحزمة بدون صور أو ارفع '
                    'MAX_STATE_TRANSFER_SIZE وMAX_UPLOAD_SIZE.'
                ),
                'code': 413,
            }

    export_dir = Path(settings.EXPORT_FOLDER)
    export_dir.mkdir(parents=True, exist_ok=True)
    suffix = '_verified' if verified_only else ''
    extension = '.xlsx' if fmt == 'xlsx' else '.json'
    filepath = reserve_artifact_path(
        export_dir, f'questions_state{suffix}', extension,
    )
    filename = download_filename(filepath)

    if fmt == 'xlsx':
        write_state_workbook(payload, filepath)
    else:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    # Never publish a state artifact this same deployment is configured to
    # reject on upload.  This check happens after serialization because base64
    # image expansion cannot be estimated reliably from model metadata alone.
    if filepath.stat().st_size > transfer_limit:
        filepath.unlink(missing_ok=True)
        return {
            'error': (
                'حجم حزمة الحالة يتجاوز حد الاستيراد. '
                'صدّر الحزمة بدون صور أو ارفع '
                'MAX_STATE_TRANSFER_SIZE وMAX_UPLOAD_SIZE.'
            ),
            'code': 413,
        }

    return {'filepath': str(filepath), 'filename': filename}
