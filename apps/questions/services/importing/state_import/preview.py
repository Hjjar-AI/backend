# backend/apps/questions/services/importing/state_import/preview.py
"""
Read-only preview passes: dry-run counters and unknown-author analysis.
"""
from ....models import Question, Category, Tag, ClinicalCase
from ..image_ingest import decode_import_image
from ..author_resolution import collect_authors_from_envelope
from .constants import STATE_IMPORT_MODE_REPLACE
from .plan import prepared_question, canonical_uuid
from ....models import clean_tag_name, CASE_GROUP_MAX_LENGTH


def _analyze_unknown_authors(
    payload,
    uuid_to_user,
    name_to_user,
    persisted_mappings,
):
    """
    Return the list of authors that could not be resolved locally.

    Each entry is { name, uuid, question_count } — enough for the
    frontend to label the modal and show the admin how many questions
    ride on each decision.
    """
    authors = collect_authors_from_envelope(payload)
    unknown = []
    for name, info in authors.items():
        author_uuid = canonical_uuid(info.get('uuid'))
        if author_uuid and author_uuid in uuid_to_user:
            continue
        if name in name_to_user:
            continue
        if name in persisted_mappings:
            continue
        unknown.append({
            'name': name,
            'uuid': author_uuid,
            'question_count': info['question_count'],
        })
    unknown.sort(key=lambda x: x['name'])
    return unknown


def _preview_state(payload, mode):
    """
    Compute the counters that a real import would report, without
    writing anything. Used by both the dry_run and analyze passes.

    COUNTERS (mirrors _apply_state)
    -------------------------------
    questions_created  — new rows (uuid not present locally)
    questions_updated  — in-place updates (replace mode only)
    questions_skipped  — merge mode only: uuid already local
    questions_deleted  — replace mode only: local PUBLIC orphans removed
    drafts_created     — subset of questions_created (drafts)
    drafts_updated     — subset of questions_updated (drafts)
    drafts_skipped     — subset of questions_skipped (drafts)

    REPLACE MODE (revised)
    ----------------------
    Replace mode is an in-place upsert keyed on uuid. The preview
    therefore reports:
      • questions_updated  for every envelope entry whose uuid already
                           exists locally (PK preserved → through-FK
                           rows survive).
      • questions_created  for every envelope entry whose uuid is new.
      • questions_deleted  for every local PUBLIC question
                           (is_draft=False) whose uuid is not in the
                           envelope — the orphan set. Local drafts
                           not in the envelope SURVIVE and are not
                           counted here.

    Prior to this revision, replace mode deleted every public
    question and recreated every envelope entry under a fresh PK.
    The preview counted every envelope entry as "created" and did
    not surface the delete side at all — the admin saw "500
    questions created" for an import that had also just wiped every
    bookmark, flag, rating, and SRS row pointing at those questions.

    MERGE MODE
    ----------
    questions_skipped counts envelope entries whose uuid already
    exists locally. Merge mode does not touch existing rows.
    """
    preview = {
        'categories_created': 0,
        'categories_updated': 0,
        'tags_created': 0,
        'cases_created': 0,
        'questions_created': 0,
        'questions_updated': 0,
        'questions_skipped': 0,
        'questions_deleted': 0,
        'images_imported': 0,
        'drafts_created': 0,
        'drafts_updated': 0,
        'drafts_skipped': 0,
    }

    existing_category_uuids = set(
        str(u) for u in Category.objects.values_list('uuid', flat=True)
    )
    existing_category_names = set(Category.objects.values_list('name', flat=True))
    for cat in payload.get('categories') or []:
        uuid_str = canonical_uuid(cat.get('uuid'))
        name = (cat.get('name') or '').strip()
        if not uuid_str or not name:
            continue
        if uuid_str in existing_category_uuids or name in existing_category_names:
            preview['categories_updated'] += 1
        else:
            preview['categories_created'] += 1
        existing_category_uuids.add(uuid_str)
        existing_category_names.add(name)

    existing_tag_uuids = set(
        str(u) for u in Tag.objects.values_list('uuid', flat=True)
    )
    existing_tag_names = set(Tag.objects.values_list('name', flat=True))
    for tag in payload.get('tags') or []:
        uuid_str = canonical_uuid(tag.get('uuid'))
        name = clean_tag_name(tag.get('name') or '')
        if not uuid_str or not name:
            continue
        if uuid_str not in existing_tag_uuids and name not in existing_tag_names:
            preview['tags_created'] += 1
        existing_tag_uuids.add(uuid_str)
        existing_tag_names.add(name)

    existing_case_uuids = set(
        str(u) for u in ClinicalCase.objects.values_list('uuid', flat=True)
    )
    existing_case_keys = set(ClinicalCase.objects.values_list('key', flat=True))
    for case in payload.get('cases') or []:
        uuid_str = canonical_uuid(case.get('uuid'))
        key = (case.get('key') or '').strip()[:CASE_GROUP_MAX_LENGTH]
        if not uuid_str or not key:
            continue
        if uuid_str not in existing_case_uuids and key not in existing_case_keys:
            preview['cases_created'] += 1
        existing_case_uuids.add(uuid_str)
        existing_case_keys.add(key)

    question_entries = payload.get('questions') or []

    # Envelope uuids — one pass for both modes.
    envelope_uuids = [
        canonical_uuid(entry.get('uuid'))
        for entry in question_entries
    ]
    envelope_uuids = [u for u in envelope_uuids if u]

    # Existing local questions that match any envelope uuid. One
    # query, bounded by envelope size.
    existing_q_uuids = set()
    if envelope_uuids:
        existing_q_uuids = set(
            str(u) for u in Question.objects
            .filter(uuid__in=envelope_uuids)
            .values_list('uuid', flat=True)
        )

    if mode == STATE_IMPORT_MODE_REPLACE:
        # Orphan set: local PUBLIC questions (is_draft=False) whose
        # uuid is not in the envelope. This is the count of rows the
        # write pass will delete (and therefore the count of
        # Bookmark / QuestionFlag / QuestionRating / UserQuestionAttempt
        # rows that will be CASCADEd away as a consequence). Local
        # drafts not in the envelope survive and are not counted.
        preview['questions_deleted'] = (
            Question.objects
            .filter(is_draft=False)
            .exclude(uuid__in=envelope_uuids)
            .count()
        )

        for entry in question_entries:
            prepared = prepared_question(entry)
            if prepared is None:
                continue
            uuid_str = prepared['uuid']
            is_draft = prepared['is_draft']
            if uuid_str in existing_q_uuids:
                preview['questions_updated'] += 1
                if is_draft:
                    preview['drafts_updated'] += 1
            else:
                preview['questions_created'] += 1
                if is_draft:
                    preview['drafts_created'] += 1
    else:
        # Merge mode: skip anything whose uuid already exists.
        for entry in question_entries:
            prepared = prepared_question(entry)
            if prepared is None:
                continue
            uuid_str = prepared['uuid']
            is_draft = prepared['is_draft']
            if uuid_str in existing_q_uuids:
                preview['questions_skipped'] += 1
                if is_draft:
                    preview['drafts_skipped'] += 1
            else:
                preview['questions_created'] += 1
                if is_draft:
                    preview['drafts_created'] += 1

    for entry in question_entries:
        prepared = prepared_question(entry)
        if prepared is None:
            continue
        if mode != STATE_IMPORT_MODE_REPLACE and prepared['uuid'] in existing_q_uuids:
            continue
        img = entry.get('image')
        if isinstance(img, dict) and img.get('data_base64') and decode_import_image(img) is not None:
            preview['images_imported'] += 1

    return preview
