# backend/apps/questions/services/importing/state_import/apply.py
"""
Write pass for the state-import path.

REPLACE MODE — IN-PLACE UPSERT (revised)
----------------------------------------
Replace mode used to delete every public question and then re-create
each envelope entry under a fresh integer PK. That unconditionally
CASCADEd every `Bookmark`, `QuestionFlag`, `QuestionRating`, and
`UserQuestionAttempt` row pointing at any public question — because
all four FKs use `on_delete=CASCADE` on `Question` — and the same
data could not be re-associated afterward because the re-import
produces the same uuids but new PKs. An admin running a "restore
the question bank from backup" import therefore silently wiped
every user's bookmarks, flags, ratings, and SRS schedule for the
affected questions.

Replace mode is now an upsert keyed on `uuid`:

  • An envelope entry whose uuid already exists locally is UPDATED
    in place — the integer PK is preserved, so every through-FK row
    referencing that question survives (bookmarks, flags, ratings,
    SRS attempts).
  • An envelope entry whose uuid is new is CREATED.
  • A local PUBLIC question (`is_draft=False`) whose uuid is NOT in
    the envelope is an ORPHAN and is deleted. This is the only place
    the CASCADE to Bookmark / Flag / Rating / SRS fires, and it
    fires only for public questions the envelope does not cover —
    which is the honest meaning of "replace the public bank."
  • A local DRAFT whose uuid is NOT in the envelope SURVIVES. Drafts
    are per-author working state; the admin running a state import
    to replace the question bank is replacing the public bank, not
    every other author's drafts. This matches the previous
    revision's documented intent.

The MasterExamQuestion pre-flight is scoped to the orphan set: a
live exam that references a question the envelope is merely
updating does not block the import (the FK target survives), but a
live exam that references an orphan does, because PROTECT on the
through-FK would refuse the orphan delete.

SINGLE CREATE HELPER (fix — two byte-identical 18-line blocks)
--------------------------------------------------------------
The "merge mode, new uuid → create" branch and the "replace mode,
uuid not found → CREATE" branch used to inline the same
`Question.objects.create(...)` call, 40 lines apart, byte-identical
in all 16 keyword arguments. Any future field addition to that
create path had to be made twice or it silently applied to only one
mode. Both call sites now use `_create_question_from_entry`.
"""

import logging
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from ....models import (
    Question,
    Category,
    Tag,
    ClinicalCase,
    clean_tag_name,
    CASE_STEM_MAX_LENGTH,
    CASE_GROUP_MAX_LENGTH,
)
from ..author_resolution import resolve_author
from ..image_ingest import apply_image
from ..validators import parse_difficulty
from .constants import STATE_IMPORT_MODE_REPLACE
from .identity import _find_existing_by_uuid_or_key
from .plan import prepared_question, canonical_uuid

logger = logging.getLogger(__name__)


def _parse_optional_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _restore_question_timestamps(question, entry):
    """Restore source audit timestamps without triggering ``auto_now``."""
    updates = {}
    for field in ('created_at', 'updated_at'):
        parsed = _parse_optional_datetime(entry.get(field))
        if parsed is not None:
            updates[field] = parsed
    if updates:
        Question.objects.filter(pk=question.pk).update(**updates)


def _create_question_from_entry(
    *,
    entry,
    prepared,
    difficulty,
    category,
    case,
    author,
    acting_user,
    verified_at,
):
    """
    Create one `Question` from a validated envelope entry.

    This is the ONE place the create call lives. Before it existed,
    the same call was inlined in `_apply_state` twice — merge mode
    and replace mode each had their own copy. The two were
    byte-identical in every keyword; only the surrounding `if/else`
    branch differed. Any future field addition to the create path
    would have had to be made in both, or it would silently apply
    to only one of the two modes.

    Ownership is always the acting admin. Draft ownership is the
    acting admin when the imported question is a draft, and NULL
    otherwise. Authorship is whatever `resolve_author` returned.
    """
    is_draft = prepared['is_draft']
    return Question.objects.create(
        uuid=prepared['uuid'],
        question=prepared['question'],
        choices=prepared['choices'],
        correct_answer=prepared['correct_answer'],
        explanation=entry.get('explanation') or '',
        source=(entry.get('source') or '')[:200] or None,
        difficulty=difficulty,
        category=category,
        case=case,
        case_order=entry.get('case_order') or None,
        is_draft=is_draft,
        draft_owner=acting_user if is_draft else None,
        verified=bool(entry.get('verified', False)),
        verified_by=entry.get('verified_by') or None,
        verified_at=verified_at,
        verification_notes=entry.get('verification_notes') or None,
        authored_by=author,
        owned_by=acting_user,
        updated_by=(entry.get('updated_by') or '')[:80] or None,
        times_answered=entry.get('times_answered', 0),
        times_correct=entry.get('times_correct', 0),
        version=entry.get('version', 1),
    )


def _apply_state(
    payload,
    username,
    acting_user,
    mode,
    uuid_to_user,
    name_to_user,
    persisted_mappings,
    call_mappings,
):
    """
    Write the envelope to the database. Runs inside the caller's outer
    transaction so a failure here rolls back the mapping decisions
    persisted by persist_mapping_decisions too.

    Returns a counts dict that includes author-resolution breakdown
    (local / mapped / unresolved) on top of the preview counters.

    COUNTERS
    --------
    questions_created    — new rows (uuid not present locally)
    questions_updated    — in-place updates (replace mode only)
    questions_skipped    — merge mode only: uuid already local
    questions_deleted    — replace mode only: local PUBLIC orphans removed
    drafts_created       — subset of questions_created (drafts)
    drafts_updated       — subset of questions_updated (drafts)
    drafts_skipped       — subset of questions_skipped (drafts)
    images_imported      — questions that received an image blob

    TRUST RECOMPUTE
    ---------------
    The previous revision called `acting_user.update_trust_score()`
    only. But per-question authorship in this path comes from
    `resolve_author()`, which returns a *different* local user
    whenever the envelope's author matches an existing account by
    uuid, name, or a stored mapping.

    The write pass now collects every author whose question set was
    touched (created, updated, or removed), and calls
    `QuestionService._recompute_author_trust` on that set, matching
    the batch recompute that `bulk_verify` / `bulk_unverify` already
    use for the same reason. The acting user is always included.
    """
    counts = {
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
        'questions_with_local_author': 0,
        'questions_with_mapped_author': 0,
        'questions_with_unresolved_author': 0,
    }

    # Authors whose authored_by set may have changed. Recomputed in
    # one batch after the write loop, mirroring the pattern in
    # QuestionService.bulk_verify.
    affected_author_ids = set()

    with transaction.atomic():
        # ── Categories ────────────────────────────────────────────
        cat_by_uuid = {}
        for entry in payload.get('categories') or []:
            uuid_str = canonical_uuid(entry.get('uuid'))
            name = (entry.get('name') or '').strip()
            if not uuid_str or not name:
                continue

            existing, matched_by = _find_existing_by_uuid_or_key(
                Category, uuid_str, 'name', name,
            )
            if existing is None:
                cat = Category.objects.create(
                    uuid=uuid_str,
                    name=name,
                    description=entry.get('description') or '',
                    color=entry.get('color') or '#667eea',
                    icon=entry.get('icon') or 'bi-folder',
                )
                counts['categories_created'] += 1
            else:
                cat = existing
                if matched_by == 'key':
                    logger.info(
                        'Category "%s" matched by name with different '
                        'uuid (local=%s, envelope=%s) — keeping local uuid',
                        name, cat.uuid, uuid_str,
                    )
                cat.name = name
                cat.description = entry.get('description') or cat.description
                cat.color = entry.get('color') or cat.color
                cat.icon = entry.get('icon') or cat.icon
                cat.save(update_fields=[
                    'name', 'description', 'color', 'icon', 'updated_at',
                ])
                counts['categories_updated'] += 1
            cat_by_uuid[uuid_str] = cat

        # ── Tags ──────────────────────────────────────────────────
        tag_by_uuid = {}
        for entry in payload.get('tags') or []:
            uuid_str = canonical_uuid(entry.get('uuid'))
            name = clean_tag_name(entry.get('name') or '')
            if not uuid_str or not name:
                continue

            existing, matched_by = _find_existing_by_uuid_or_key(
                Tag, uuid_str, 'name', name,
            )
            if existing is None:
                tag = Tag.objects.create(uuid=uuid_str, name=name)
                counts['tags_created'] += 1
            else:
                tag = existing
                if matched_by == 'key':
                    logger.info(
                        'Tag "%s" matched by name with different uuid '
                        '(local=%s, envelope=%s) — keeping local uuid',
                        name, tag.uuid, uuid_str,
                    )
            tag_by_uuid[uuid_str] = tag

        # Second pass: wire parent relationships now that every tag
        # referenced by the envelope exists in tag_by_uuid.
        for entry in payload.get('tags') or []:
            uuid_str = canonical_uuid(entry.get('uuid'))
            parent_uuid = canonical_uuid(entry.get('parent_uuid'))
            if not parent_uuid:
                continue
            child = tag_by_uuid.get(uuid_str)
            parent = tag_by_uuid.get(parent_uuid)
            if child and parent and child.parent_id != parent.id:
                child.parent = parent
                child.save(update_fields=['parent'])

        # ── Cases ─────────────────────────────────────────────────
        case_by_uuid = {}
        for entry in payload.get('cases') or []:
            uuid_str = canonical_uuid(entry.get('uuid'))
            key = (entry.get('key') or '').strip()[:CASE_GROUP_MAX_LENGTH]
            if not uuid_str or not key:
                continue
            stem = (entry.get('stem') or '').strip()[:CASE_STEM_MAX_LENGTH] or None

            case_author, _case_source = resolve_author(
                name=(entry.get('authored_by_name') or '').strip() or None,
                author_uuid=canonical_uuid(entry.get('authored_by_uuid')) or None,
                uuid_to_user=uuid_to_user,
                name_to_user=name_to_user,
                persisted_mappings=persisted_mappings,
                call_mappings=call_mappings,
                acting_user=acting_user,
            )
            if case_author is None:
                case_author = acting_user

            existing, matched_by = _find_existing_by_uuid_or_key(
                ClinicalCase, uuid_str, 'key', key,
            )
            if existing is None:
                case = ClinicalCase.objects.create(
                    uuid=uuid_str,
                    key=key,
                    title=(entry.get('title') or '').strip() or None,
                    stem=stem,
                    authored_by=case_author,
                )
                counts['cases_created'] += 1
            else:
                case = existing
                if matched_by == 'key':
                    logger.info(
                        'Case "%s" matched by key with different uuid '
                        '(local=%s, envelope=%s) — keeping local uuid',
                        key, case.uuid, uuid_str,
                    )
                dirty = []
                if not case.stem and stem:
                    case.stem = stem
                    dirty.append('stem')
                if not case.title and entry.get('title'):
                    case.title = (entry.get('title') or '').strip() or None
                    dirty.append('title')
                if dirty:
                    dirty.append('updated_at')
                    case.save(update_fields=dirty)
            case_by_uuid[uuid_str] = case

        # ── Replace mode: orphan detection and delete ─────────────
        if mode == STATE_IMPORT_MODE_REPLACE:
            envelope_uuids = {
                canonical_uuid(entry.get('uuid'))
                for entry in payload.get('questions') or []
                if canonical_uuid(entry.get('uuid'))
            }
            orphans_qs = (
                Question.objects
                .filter(is_draft=False)
                .exclude(uuid__in=envelope_uuids)
            )
            orphan_ids = list(orphans_qs.values_list('id', flat=True))

            if orphan_ids:
                from apps.master_exams.models import MasterExamQuestion
                blocked = MasterExamQuestion.objects.filter(
                    question_id__in=orphan_ids,
                ).exists()
                if blocked:
                    raise ValueError('REPLACE_BLOCKED_BY_MASTER_EXAMS')

                orphan_author_ids = list(
                    Question.objects
                    .filter(id__in=orphan_ids, authored_by__isnull=False)
                    .values_list('authored_by_id', flat=True)
                    .distinct()
                )
                affected_author_ids.update(orphan_author_ids)

                Question.objects.filter(id__in=orphan_ids).delete()
                counts['questions_deleted'] = len(orphan_ids)

        # ── Pre-fetch existing questions by uuid ──────────────────
        envelope_uuids_list = []
        for entry in payload.get('questions') or []:
            u = canonical_uuid(entry.get('uuid'))
            if u:
                envelope_uuids_list.append(u)

        existing_q_by_uuid = {}
        if envelope_uuids_list:
            existing_q_by_uuid = {
                str(q.uuid): q
                for q in Question.objects.filter(uuid__in=envelope_uuids_list)
            }

        # ── Per-question upsert ───────────────────────────────────
        for entry in payload.get('questions') or []:
            prepared = prepared_question(entry)
            if prepared is None:
                continue
            uuid_str = prepared['uuid']

            difficulty = parse_difficulty(
                entry.get('difficulty', 'medium'),
            )

            category = cat_by_uuid.get(
                canonical_uuid(entry.get('category_uuid'))
            )
            case = case_by_uuid.get(
                canonical_uuid(entry.get('case_uuid'))
            )

            author, source = resolve_author(
                name=(entry.get('authored_by_name') or '').strip() or None,
                author_uuid=canonical_uuid(entry.get('authored_by_uuid')) or None,
                uuid_to_user=uuid_to_user,
                name_to_user=name_to_user,
                persisted_mappings=persisted_mappings,
                call_mappings=call_mappings,
                acting_user=acting_user,
            )
            if source in ('local_uuid', 'local_name'):
                counts['questions_with_local_author'] += 1
            elif source == 'mapping':
                counts['questions_with_mapped_author'] += 1
            else:
                counts['questions_with_unresolved_author'] += 1

            verified_at = _parse_optional_datetime(entry.get('verified_at'))

            existing_q = existing_q_by_uuid.get(uuid_str)

            # ── Merge mode: skip existing, create new ─────────────
            if mode != STATE_IMPORT_MODE_REPLACE:
                if existing_q is not None:
                    counts['questions_skipped'] += 1
                    if prepared['is_draft']:
                        counts['drafts_skipped'] += 1
                    continue

                q = _create_question_from_entry(
                    entry=entry,
                    prepared=prepared,
                    difficulty=difficulty,
                    category=category,
                    case=case,
                    author=author,
                    acting_user=acting_user,
                    verified_at=verified_at,
                )
                counts['questions_created'] += 1
                if prepared['is_draft']:
                    counts['drafts_created'] += 1

            # ── Replace mode: upsert ──────────────────────────────
            else:
                if existing_q is not None:
                    # UPDATE in place. Record the previous author so
                    # a re-attribution refreshes BOTH users' scores.
                    if existing_q.authored_by_id is not None:
                        affected_author_ids.add(existing_q.authored_by_id)

                    is_draft = prepared['is_draft']
                    existing_q.question = prepared['question']
                    existing_q.choices = prepared['choices']
                    existing_q.correct_answer = prepared['correct_answer']
                    existing_q.explanation = entry.get('explanation') or ''
                    existing_q.source = (entry.get('source') or '')[:200] or None
                    existing_q.difficulty = difficulty
                    existing_q.category = category
                    existing_q.case = case
                    existing_q.case_order = entry.get('case_order') or None
                    existing_q.is_draft = is_draft
                    existing_q.draft_owner = acting_user if is_draft else None
                    existing_q.verified = bool(entry.get('verified', False))
                    existing_q.verified_by = entry.get('verified_by') or None
                    existing_q.verified_at = verified_at
                    existing_q.verification_notes = (
                        entry.get('verification_notes') or None
                    )
                    existing_q.authored_by = author
                    existing_q.owned_by = acting_user
                    # These fields were added additively to v2. Preserve
                    # local values when importing an older v2 envelope that
                    # predates them; current exports always include them.
                    if 'updated_by' in entry:
                        existing_q.updated_by = (
                            (entry.get('updated_by') or '')[:80] or None
                        )
                    if 'times_answered' in entry:
                        existing_q.times_answered = entry['times_answered']
                    if 'times_correct' in entry:
                        existing_q.times_correct = entry['times_correct']
                    if 'version' in entry:
                        existing_q.version = entry['version']
                    existing_q.save()

                    q = existing_q
                    counts['questions_updated'] += 1
                    if is_draft:
                        counts['drafts_updated'] += 1
                else:
                    q = _create_question_from_entry(
                        entry=entry,
                        prepared=prepared,
                        difficulty=difficulty,
                        category=category,
                        case=case,
                        author=author,
                        acting_user=acting_user,
                        verified_at=verified_at,
                    )
                    counts['questions_created'] += 1
                    if prepared['is_draft']:
                        counts['drafts_created'] += 1

            if author is not None:
                affected_author_ids.add(author.id)

            tag_objs = []
            for tag_uuid in entry.get('tags') or []:
                tag = tag_by_uuid.get(canonical_uuid(tag_uuid))
                if tag is not None:
                    tag_objs.append(tag)
            q.tags.set(tag_objs)

            image_block = entry.get('image')
            if isinstance(image_block, dict) and image_block.get('data_base64'):
                saved = apply_image(q, image_block)
                if saved:
                    counts['images_imported'] += 1

            _restore_question_timestamps(q, entry)

    # ── Trust recompute (batch) ───────────────────────────────────
    affected_author_ids.add(acting_user.id)
    if affected_author_ids:
        from apps.questions.services.question_service import QuestionService
        QuestionService._recompute_author_trust(list(affected_author_ids))

    return counts
