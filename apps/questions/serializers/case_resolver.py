# backend/apps/questions/serializers/case_resolver.py
"""
Case-resolution helper used by every write path that can attach a
question to a ClinicalCase.

This lives in its own module (not inside `case.py`) because it is a
write-path helper, not a serializer, and it is imported directly by
non-serializer code:

  • apps/master_exams/services/master_exam_service.py
  • apps/master_exams/views/drafts_views.py
  • apps/questions/services/question_service.py

Keeping it separate makes that cross-module contract visible and
lets the helper be exercised in isolation.
"""

from ..models import ClinicalCase


def _resolve_case(case_key, user, *, stem=None, existing_case=None):
    """
    Resolve a case_key string to a ClinicalCase instance, creating the
    case on first reference. `case_key` may be None or empty, in which
    case the question is detached (returns None).

    `stem` is an optional vignette. It is applied:

      • On case creation, as the initial stem.
      • On an existing case that has no stem yet.

    It is NOT applied to a case that already has a stem. A stem edit
    on a populated case goes through the dedicated case-stem endpoint
    so concurrent edits serialize on the case row rather than racing
    on individual questions.

    `existing_case` is the case the question already points at, used
    to skip the get_or_create round-trip when the key is unchanged.
    """
    if case_key is None:
        return None
    key = str(case_key).strip()
    if not key:
        return None

    normalized_stem = None
    if stem:
        normalized_stem = str(stem).strip() or None

    if existing_case is not None and existing_case.key == key:
        if normalized_stem and not existing_case.stem:
            existing_case.stem = normalized_stem
            existing_case.save(update_fields=['stem', 'updated_at'])
        return existing_case

    created_by_user = user if getattr(user, 'is_authenticated', False) else None
    case, created = ClinicalCase.objects.get_or_create(
        key=key,
        defaults={
            'authored_by': created_by_user,
            'stem': normalized_stem,
        },
    )
    if not created and normalized_stem and not case.stem:
        case.stem = normalized_stem
        case.save(update_fields=['stem', 'updated_at'])
    return case