# backend/apps/master_exams/services/master_exam_service/composition.py
"""
Question composition: add / remove / reorder, plus inline draft creation.

OPTIMISTIC LOCKING
------------------
Every mutating function here takes an optional `expected_version`.
When supplied, the function performs a single CAS against
`MasterExam.version` before touching the through table, and raises
`MODIFIED_BY_ANOTHER_USER` on a stale read. When not supplied, the
function still bumps the version so any other caller that DID supply
an `expected_version` sees a conflict on its next attempt — a client
that omits the parameter loses the guarantee for its own request but
cannot silently leave the version behind everyone else's view of the
exam.

The version is bumped by the same CAS statement that checks it, in
the same transaction that writes the through-table rows, so a
concurrent composition edit cannot slip between the check and the
write.
"""
import logging

from django.db import transaction
from django.db.models import F, Max

from apps.questions.models import ClinicalCase, Question
from apps.core.audit import log_privileged_action

from ...models import MasterExam, MasterExamQuestion

logger = logging.getLogger(__name__)


def _cas_bump_version(exam, expected_version):
    """
    Advance `MasterExam.version` by one, rejecting a stale
    `expected_version` when one was supplied.

    Returns the new version integer so the caller can log it or
    refresh the in-memory `exam` instance if desired.

    The CAS is a single UPDATE against `(pk, version=expected)` with
    `version = version + 1`. Under MariaDB/MySQL the row lock taken
    by the UPDATE serializes concurrent composers; under SQLite the
    whole UPDATE is serialized by the file lock. Either way, at most
    one of two concurrent callers with the same `expected_version`
    observes `rows == 1`.

    When `expected_version` is None the statement is unconditional and
    always affects the row — the caller gains no conflict protection
    for its own edit but the version still advances, which is what
    makes a mixed deployment (some callers checking, some not)
    safe for the checking callers.
    """
    if expected_version is None:
        MasterExam.objects.filter(pk=exam.pk).update(version=F('version') + 1)
    else:
        rows = MasterExam.objects.filter(
            pk=exam.pk, version=int(expected_version),
        ).update(version=F('version') + 1)
        if rows == 0:
            raise ValueError('MODIFIED_BY_ANOTHER_USER')
    exam.refresh_from_db(fields=['version'])
    return exam.version


def add_questions(exam, question_ids, request=None, expected_version=None):
    if not exam.can_edit_now:
        raise ValueError('EXAM_WINDOW_STARTED')

    existing_ids = set(
        exam.exam_questions.values_list('question_id', flat=True)
    )
    cleaned = []
    seen = set(existing_ids)
    for qid in question_ids:
        try:
            qid_int = int(qid)
        except (TypeError, ValueError):
            continue
        if qid_int in seen:
            continue
        cleaned.append(qid_int)
        seen.add(qid_int)

    if not cleaned:
        return exam

    caller = getattr(request, 'user', None) if request is not None else None
    visible_qs = Question.objects.filter(id__in=cleaned)
    if caller is not None:
        visible_qs = visible_qs.visible_to(caller)
    visible_ids = set(visible_qs.values_list('id', flat=True))
    missing = [qid for qid in cleaned if qid not in visible_ids]
    if missing:
        raise ValueError(f'QUESTIONS_NOT_ACCESSIBLE:{missing}')

    with transaction.atomic():
        _cas_bump_version(exam, expected_version)

        current_max = (
            exam.exam_questions.aggregate(m=Max('order'))['m'] or 0
        )
        MasterExamQuestion.objects.bulk_create(
            [
                MasterExamQuestion(
                    master_exam=exam,
                    question_id=qid,
                    order=current_max + i + 1,
                )
                for i, qid in enumerate(cleaned)
            ],
            ignore_conflicts=True,
        )

    if request is not None:
        log_privileged_action(
            request,
            action='master_exam.add_questions',
            target=exam,
            target_repr=exam.name,
            details={'count': len(cleaned), 'version': exam.version},
        )

    return exam


def remove_question(exam, question_id, request=None, expected_version=None):
    if not exam.can_edit_now:
        raise ValueError('EXAM_WINDOW_STARTED')

    try:
        qid = int(question_id)
    except (TypeError, ValueError):
        raise ValueError('INVALID_QUESTION_ID')

    with transaction.atomic():
        _cas_bump_version(exam, expected_version)
        deleted, _ = exam.exam_questions.filter(question_id=qid).delete()

    if deleted == 0:
        # The CAS still advanced the version — the caller's edit was
        # a no-op on the through table but the version moved, which
        # is the honest state: another client that held the old
        # version now sees a conflict on its next attempt, matching
        # what its in-memory view of the exam already believes.
        pass

    if request is not None:
        log_privileged_action(
            request,
            action='master_exam.remove_question',
            target=exam,
            target_repr=exam.name,
            details={'question_id': qid, 'version': exam.version},
        )

    return exam


def reorder_questions(exam, ordered_ids, request=None, expected_version=None):
    if not exam.can_edit_now:
        raise ValueError('EXAM_WINDOW_STARTED')

    current = list(
        exam.exam_questions.values_list('question_id', flat=True)
    )
    try:
        requested = [int(q) for q in ordered_ids]
    except (TypeError, ValueError):
        raise ValueError('INVALID_QUESTION_IDS')

    if sorted(requested) != sorted(current):
        raise ValueError('REORDER_MISMATCH')

    from django.db.models import Case, When, Value, IntegerField
    when_clauses = [
        When(question_id=qid, then=Value(i + 1))
        for i, qid in enumerate(requested)
    ]
    with transaction.atomic():
        _cas_bump_version(exam, expected_version)
        exam.exam_questions.update(
            order=Case(*when_clauses, output_field=IntegerField())
        )

    if request is not None:
        log_privileged_action(
            request,
            action='master_exam.reorder',
            target=exam,
            target_repr=exam.name,
            details={'count': len(requested), 'version': exam.version},
        )

    return exam


def add_draft(exam, draft_payload, author, request=None, expected_version=None):

    if not exam.can_edit_now:
        raise ValueError('EXAM_WINDOW_STARTED')

    case_key = draft_payload.pop('case_key', None)
    case_stem = draft_payload.pop('case_stem', None)
    case_order = draft_payload.pop('case_order', None)

    case = None
    if case_key:
        # Reuse the same resolver the question serializers use, so
        # the stem-fill semantics are identical across every write
        # path that can create a case.
        from apps.questions.serializers import _resolve_case
        case = _resolve_case(case_key, author, stem=case_stem)

    with transaction.atomic():
        _cas_bump_version(exam, expected_version)

        draft = Question.objects.create(
            question=draft_payload['question'],
            choices=draft_payload['choices'],
            correct_answer=draft_payload['correct_answer'],
            explanation=draft_payload.get('explanation') or '',
            source=draft_payload.get('source') or None,
            difficulty=draft_payload.get('difficulty', 'medium'),
            category=draft_payload.get('category'),
            is_draft=True,
            draft_owner=author,
            authored_by=author,
            owned_by=author,
            verified=False,
            case=case,
            case_order=case_order,
        )

        current_max = (
            exam.exam_questions.aggregate(m=Max('order'))['m'] or 0
        )
        MasterExamQuestion.objects.create(
            master_exam=exam,
            question=draft,
            order=current_max + 1,
        )

    if request is not None:
        log_privileged_action(
            request,
            action='master_exam.add_draft',
            target=draft,
            target_repr=f'draft:{draft.id}',
            details={
                'exam_id': exam.id,
                'exam_name': exam.name,
                'version': exam.version,
            },
        )

    return draft