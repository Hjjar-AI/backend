"""Conflict discovery and per-question conflict decisions."""

from ....models import Question
from .constants import CONFLICT_REVIEW, CONFLICT_USE_IMPORTED
from .plan import canonical_uuid


CONFLICT_FIELDS = (
    'question', 'choices', 'correct_answer', 'explanation', 'source',
    'source_document', 'source_page', 'translations', 'difficulty',
    'category_uuid', 'tags', 'case_uuid', 'case_order', 'is_draft',
    'verified', 'verified_by', 'verified_at', 'verification_notes',
    'updated_by', 'times_answered', 'times_correct', 'version',
)


def _empty_to_none(value):
    return None if value in ('', None) else value


def _local_value(question, field):
    if field == 'category_uuid':
        return str(question.category.uuid) if question.category_id else None
    if field == 'case_uuid':
        return str(question.case.uuid) if question.case_id else None
    if field == 'tags':
        return sorted(str(tag.uuid) for tag in question.tags.all())
    if field == 'verified_at':
        return question.verified_at.isoformat() if question.verified_at else None
    value = getattr(question, field, None)
    if field in {
        'explanation', 'source', 'source_document', 'verified_by',
        'verification_notes', 'updated_by',
    }:
        return _empty_to_none(value)
    if field == 'translations':
        return value or {}
    return value


def _incoming_value(entry, field, local_value):
    # Additive v3 fields absent from a migrated v2 package mean "preserve
    # local", not "clear". Excluding them from the diff keeps old packages
    # backward compatible.
    if field not in entry:
        return local_value
    value = entry.get(field)
    if field == 'tags':
        return sorted(canonical_uuid(item) for item in (value or []))
    if field in {
        'category_uuid', 'case_uuid',
    }:
        return canonical_uuid(value) or None
    if field in {
        'explanation', 'source', 'source_document', 'verified_by',
        'verification_notes', 'updated_by', 'verified_at',
    }:
        return _empty_to_none(value)
    if field == 'translations':
        return value or {}
    return value


def analyze_conflicts(payload):
    entries = {
        canonical_uuid(entry.get('uuid')): entry
        for entry in payload.get('questions') or []
        if canonical_uuid(entry.get('uuid'))
    }
    if not entries:
        return []
    existing = (
        Question.objects
        .filter(uuid__in=entries)
        .select_related('category', 'case')
        .prefetch_related('tags')
    )
    conflicts = []
    for question in existing:
        uuid_str = str(question.uuid)
        entry = entries[uuid_str]
        changed = []
        for field in CONFLICT_FIELDS:
            local = _local_value(question, field)
            incoming = _incoming_value(entry, field, local)
            if incoming != local:
                changed.append(field)
        if changed:
            conflicts.append({
                'uuid': uuid_str,
                'local_question': question.question,
                'imported_question': entry.get('question') or '',
                'changed_fields': changed,
            })
    conflicts.sort(key=lambda item: item['local_question'].casefold())
    return conflicts


def use_imported_for(uuid_str, strategy, resolutions):
    if strategy == CONFLICT_USE_IMPORTED:
        return True
    if strategy == CONFLICT_REVIEW:
        return (resolutions or {}).get(uuid_str) == CONFLICT_USE_IMPORTED
    return False
