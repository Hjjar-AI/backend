# backend/apps/master_exams/services/master_exam_service/__init__.py
"""
Package surface for MasterExamService.

Split across:

  • constants.py    — DELETE_MODE_* identifiers
  • crud.py         — create / update
  • composition.py  — add / remove / reorder questions, add_draft
  • lifecycle.py    — publish / cancel / publish_to_bank / delete
  • audience.py     — resolve_audience, access checks, acknowledgement

`MasterExamService` is a thin façade that re-exports every public
method as a static method, so existing call sites
(`MasterExamService.create(...)`, `MasterExamService.add_questions(...)`,
`MasterExamService._assigned_exam_ids(...)`, etc.) keep working
unchanged.

Public module surface preserved from the pre-split file:

    from apps.master_exams.services import MasterExamService
    MasterExamService.<method>(...)

The leading-underscore methods (`_assigned_exam_ids`) are bound as
static methods on the class because `crud_views.py` reads them
through the façade.
"""
from .constants import (
    DELETE_MODE_DELETE,
    DELETE_MODE_KEEP,
    DELETE_MODE_PUBLISH,
    VALID_DELETE_MODES,
)
from . import crud, composition, lifecycle, audience


class MasterExamService:
    # ── CRUD ──────────────────────────────────────────────────────
    create = staticmethod(crud.create)
    update = staticmethod(crud.update)

    # ── Composition ───────────────────────────────────────────────
    add_questions = staticmethod(composition.add_questions)
    remove_question = staticmethod(composition.remove_question)
    reorder_questions = staticmethod(composition.reorder_questions)
    add_draft = staticmethod(composition.add_draft)

    # ── Lifecycle ─────────────────────────────────────────────────
    publish = staticmethod(lifecycle.publish)
    cancel = staticmethod(lifecycle.cancel)
    publish_to_bank = staticmethod(lifecycle.publish_to_bank)
    delete = staticmethod(lifecycle.delete)

    # ── Audience & acknowledgement ────────────────────────────────
    resolve_audience = staticmethod(audience.resolve_audience)
    user_can_access = staticmethod(audience.user_can_access)
    user_has_attempt = staticmethod(audience.user_has_attempt)
    user_completed_attempt = staticmethod(audience.user_completed_attempt)
    acknowledge = staticmethod(audience.acknowledge)
    needs_acknowledgement = staticmethod(audience.needs_acknowledgement)
    _assigned_exam_ids = staticmethod(audience._assigned_exam_ids)


__all__ = [
    'MasterExamService',
    'DELETE_MODE_DELETE',
    'DELETE_MODE_KEEP',
    'DELETE_MODE_PUBLISH',
    'VALID_DELETE_MODES',
]