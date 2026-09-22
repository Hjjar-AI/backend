# backend/apps/master_exams/services/master_exam_service/lifecycle.py
"""
Status transitions: publish, cancel, publish_to_bank, delete.

CONCURRENCY — ROW LOCK ON EVERY TRANSITION
------------------------------------------
Prior to this revision, each function here followed the same shape:

    def publish(exam, request=None):
        if exam.stored_status != 'draft':
            raise ValueError('EXAM_ALREADY_PUBLISHED')
        with transaction.atomic():
            exam.stored_status = 'published'
            exam.published_at = timezone.now()
            exam.save(update_fields=['stored_status', 'published_at'])
        ...

The guard was checked against the CALLER'S IN-MEMORY `exam` object —
fetched by a plain `get_object_or_404(MasterExam, pk=pk)` in the view,
with no row lock — and the write inside the atomic block never
re-verified that guard. Two concurrent `POST /publish` calls
(double-click, two admin tabs, a script retry) both read
`stored_status == 'draft'`, both passed the guard, both wrote, and the
last save silently won. The docstring on `MasterExamPublishView`
claimed "Rejects a second publish" — true only for strictly sequential
callers. The same race existed between `publish()` and `cancel()`, and
between any transition and a concurrent `delete()`.

Every function below now acquires a `select_for_update()` lock on the
exam row, re-reads its state under that lock, re-runs the guard, and
only then writes. This is the same serialization pattern used by
`_acquire_admin_lock_set` in `apps/users/views/admin_user_views.py`
for the equivalent admin-user transitions.

WHY `version` IS NOT BUMPED HERE
--------------------------------
`MasterExam.CONTENT_FIELDS` deliberately excludes the four lifecycle
fields (`stored_status`, `published_at`, `completed_at`,
`published_to_bank_at`). The original design rationale is documented
on the model: an optimistic lock guards CONTENT edits, and a status
transition does not change the content an editor is looking at.
Bumping `version` on a status transition would make a legitimate
in-flight content edit fail with `MODIFIED_BY_ANOTHER_USER` even
though nothing the editor cares about changed.

The row lock below is the correct fix for the concurrency concern.
Composition callers holding a stale version are already blocked by
`can_edit_now` (which refuses edits on terminal states) — see
`MasterExam.can_edit_now` and the `EXAM_WINDOW_STARTED` guard in
`composition.py`.

RETURN CONTRACT
---------------
Each transition returns the row-locked, post-save `MasterExam`
instance. Callers MUST rebind their local reference to the return
value — `exam = MasterExamService.publish(exam, request=request)` —
or they will hold stale data. `MasterExamPublishView.post` and its
siblings already do this.

`delete()` returns `None`. The row is gone and there is nothing
meaningful to return; the caller's pre-delete reference retains its
old field values, which is what the view's success message uses.

EXAM_NOT_FOUND
--------------
`_locked_exam` raises `ValueError('EXAM_NOT_FOUND')` when the row
has disappeared between the caller's `get_object_or_404` and the
row-lock acquisition. This is mapped to a 404 by
`_error_map.exam_error_response` and caught by the view's
`except ValueError`. Without this translation, the underlying
`MasterExam.DoesNotExist` would escape the view's error handler and
produce a 500 on a perfectly recoverable concurrent-delete race.

AUTHOR REPUTATION RECOMPUTE
---------------------------
`_tag_and_publish_questions_to_bank` flips `is_draft` from True to
False on a set of question rows. That transition moves those rows
INTO the set `AuthorReputationService.refresh_users` aggregates over
(it filters on `is_draft=False`), so the affected authors'
`questions_count` / `trust_score` are stale until someone recomputes
them. The helper returns the affected author ids; each caller hands
them to `QuestionService._recompute_author_trust` AFTER its own
transaction commits, so the aggregate reads the flipped state.

The recompute deliberately runs OUTSIDE the atomic block that
persisted the exam-row change: a failure in the recompute leaves the
stored counters stale (self-healing on the next `manage.py
refresh_author_ranks`), rather than rolling back the transition
itself.
"""
import logging

from django.db import transaction
from django.utils import timezone

from apps.questions.models import Question, Tag, clean_tag_name
from apps.core.audit import log_privileged_action

from ...models import MasterExam, MasterExamQuestion
from .constants import (
    DELETE_MODE_DELETE,
    DELETE_MODE_KEEP,
    DELETE_MODE_PUBLISH,
    VALID_DELETE_MODES,
)

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════
# Shared publish-to-bank routine
# ═════════════════════════════════════════════════════════════════════
#
# The "tag every question with the exam's date-topic tag and flip
# is_draft=False" block used to be duplicated verbatim between
# `publish_to_bank()` and the DELETE_MODE_PUBLISH branch of `delete()`.
# The two paths are semantically the same operation — they differ
# only in what they do with the exam row afterward (keep it vs.
# delete it). Extracting the block keeps the tag-name construction,
# the through-table pair check, and the bulk_create in one place.
#
# AUTHOR REPUTATION RECOMPUTE
# ---------------------------
# Both callers of `_tag_and_publish_questions_to_bank` flip
# `is_draft` from True to False on a set of question rows. That
# transition moves those rows INTO the set
# `AuthorReputationService.refresh_users` aggregates over (it filters
# on `is_draft=False`). Prior to this revision the flip happened via
# a raw `Question.objects.filter(...).update(is_draft=False)` and the
# authors' `questions_count` / `trust_score` were left stale until
# someone ran `manage.py refresh_author_ranks`.
#
# The helper now captures the affected author ids BEFORE the update
# and returns them alongside the tag name; each caller hands them to
# `QuestionService._recompute_author_trust` after the surrounding
# transaction commits.


def _locked_exam(exam_pk):
    """
    Re-fetch the exam with a `SELECT ... FOR UPDATE` row lock.

    Returns the locked instance. The caller MUST be inside a
    `transaction.atomic()` block — the lock is held until the
    surrounding transaction commits or rolls back.

    Raises `ValueError('EXAM_NOT_FOUND')` when the row no longer
    exists — e.g. a concurrent `delete()` ran between the caller's
    `get_object_or_404` and this line. The view layer maps this code
    to a 404 via `_error_map.exam_error_response`. Raising a bare
    `MasterExam.DoesNotExist` here would escape the view's
    `except ValueError` handler and produce a 500 on a recoverable
    race.

    Called by every lifecycle transition. The lock is what serializes
    two concurrent transitions on the same exam; re-reading the row
    under the lock is what makes the guard checks correct — the
    caller's original `exam` object may be arbitrarily stale by the
    time this line runs.
    """
    locked = (
        MasterExam.objects
        .select_for_update()
        .filter(pk=exam_pk)
        .first()
    )
    if locked is None:
        raise ValueError('EXAM_NOT_FOUND')
    return locked


def _build_bank_tag_name(exam):
    """
    Canonical tag name for an exam's bank publication.

    Format: "{YYYY-MM-DD} - امتحان - {topic}". The date is taken from
    the exam's opening time in local time. An exam with no topic tag
    falls back to the placeholder 'بدون عنوان'.
    """
    topic = (exam.exam_topic_tag or '').strip() or 'بدون عنوان'
    date_str = timezone.localtime(exam.opens_at).strftime('%Y-%m-%d')
    return clean_tag_name(f'{date_str} - امتحان - {topic}')


def _tag_and_publish_questions_to_bank(exam, qids):
    """
    Publish the given questions to the public question bank:

      • Flip is_draft=False and clear draft_owner on every question.
      • Attach the exam's date-topic tag to each question, skipping
        the through-row when the (question, tag) pair already exists.

    Runs inside the caller's transaction. Does NOT touch the exam row
    itself — see `publish_to_bank` and `delete` for the two ways this
    is consumed.

    Returns (tag_name, affected_author_ids). The caller is responsible
    for calling `QuestionService._recompute_author_trust` on the
    author ids AFTER its transaction commits — see the section note
    at the top of this module.
    """
    tag_name = _build_bank_tag_name(exam)
    tag, _ = Tag.objects.get_or_create(name=tag_name)

    if not qids:
        return tag_name, []

    # Capture the authors whose published-question set is about to
    # change. Do this BEFORE the update — the `authored_by_id` we
    # need is on the same rows being updated, but the aggregate in
    # `refresh_users` only counts `is_draft=False`, so recompute
    # has to happen after the flip.
    affected_author_ids = list(
        Question.objects
        .filter(id__in=qids, authored_by_id__isnull=False, is_draft=True)
        .values_list('authored_by_id', flat=True)
        .distinct()
    )

    Question.objects.filter(id__in=qids).update(
        is_draft=False,
        draft_owner=None,
    )

    existing_pairs = set(
        Question.objects
        .filter(id__in=qids, tags=tag)
        .values_list('id', flat=True)
    )
    through_model = Question.tags.through
    to_create = [
        through_model(question_id=qid, tag_id=tag.id)
        for qid in qids
        if qid not in existing_pairs
    ]
    if to_create:
        through_model.objects.bulk_create(
            to_create, ignore_conflicts=True,
        )

    return tag_name, affected_author_ids


# ═════════════════════════════════════════════════════════════════════
# Lifecycle transitions
# ═════════════════════════════════════════════════════════════════════


def publish(exam, request=None):
    """
    Transition draft → published.

    CONCURRENCY: row lock + re-check under lock. See the module
    docstring for the race this closes. A second concurrent call now
    blocks on the row lock, then re-reads `stored_status`, finds it
    already 'published', and raises `EXAM_ALREADY_PUBLISHED` — which
    is what the view's docstring has always claimed and what the
    previous implementation failed to deliver.

    Returns the row-locked, post-save instance. Callers MUST rebind
    to the return value.
    """
    with transaction.atomic():
        locked = _locked_exam(exam.pk)

        if locked.stored_status != 'draft':
            raise ValueError('EXAM_ALREADY_PUBLISHED')
        if not locked.exam_questions.exists():
            raise ValueError('EMPTY_EXAM')

        locked.stored_status = 'published'
        locked.published_at = timezone.now()
        locked.save(update_fields=['stored_status', 'published_at'])

        question_count = locked.exam_questions.count()

    # `locked` is the fresh post-save instance and the audit log below
    # reads its fields directly. No `refresh_from_db()` on `exam` is
    # needed — refreshing after the transaction commits would race a
    # concurrent writer and could return their state, not ours.
    if request is not None:
        log_privileged_action(
            request,
            action='master_exam.publish',
            target=locked,
            target_repr=locked.name,
            details={
                'question_count': question_count,
                'opens_at': locked.opens_at.isoformat(),
                'closes_at': locked.closes_at.isoformat(),
            },
        )

    return locked


def cancel(exam, request=None):
    """
    Transition any non-terminal status → cancelled.

    CONCURRENCY: same row-lock + re-check pattern as `publish`. A
    concurrent publish and cancel on the same exam now serialize;
    the loser either observes a terminal state and raises
    `EXAM_TERMINAL`, or a publish that lost the race is refused by
    its own `stored_status != 'draft'` guard. The audit trail no
    longer asserts a transition that did not persist.

    Returns the row-locked, post-save instance.
    """
    with transaction.atomic():
        locked = _locked_exam(exam.pk)

        if locked.stored_status in ('cancelled', 'published_to_bank'):
            raise ValueError('EXAM_TERMINAL')

        locked.stored_status = 'cancelled'
        locked.save(update_fields=['stored_status'])

    if request is not None:
        log_privileged_action(
            request,
            action='master_exam.cancel',
            target=locked,
            target_repr=locked.name,
            details={},
        )

    return locked


def publish_to_bank(exam, request=None):
    """
    Transition completed → published_to_bank.

    CONCURRENCY: row lock covers both the status guard AND the
    question-flip block. Two concurrent calls now serialize: the
    loser observes `stored_status == 'published_to_bank'` and raises
    `ALREADY_PUBLISHED_TO_BANK` instead of re-flipping the questions
    and re-tagging them.

    The guard also re-checks `timezone.now() < locked.closes_at`
    under the lock, so a call issued concurrently with an edit to
    `closes_at` cannot pass on stale timing data.

    AUTHOR RECOMPUTE runs after the transaction commits — see the
    module docstring.

    Returns the row-locked, post-save instance.
    """
    affected_author_ids = []
    qids = []
    tag_name = None

    with transaction.atomic():
        locked = _locked_exam(exam.pk)

        if locked.stored_status == 'published_to_bank':
            raise ValueError('ALREADY_PUBLISHED_TO_BANK')
        if timezone.now() < locked.closes_at:
            raise ValueError('WINDOW_NOT_CLOSED')

        qids = list(
            locked.exam_questions.values_list('question_id', flat=True)
        )

        tag_name, affected_author_ids = _tag_and_publish_questions_to_bank(
            locked, qids,
        )

        locked.stored_status = 'published_to_bank'
        locked.published_to_bank_at = timezone.now()
        locked.save(update_fields=['stored_status', 'published_to_bank_at'])

    # Recompute AFTER the transaction commits so the aggregate sees
    # the flipped is_draft values. See the section note at the top
    # of this module for the rationale.
    if affected_author_ids:
        from apps.questions.services import QuestionService
        QuestionService._recompute_author_trust(affected_author_ids)

    if request is not None:
        log_privileged_action(
            request,
            action='master_exam.publish_to_bank',
            target=locked,
            target_repr=locked.name,
            details={
                'question_count': len(qids),
                'tag': tag_name,
                'authors_recomputed': len(affected_author_ids),
            },
        )

    return locked


def delete(exam, delete_mode, request=None):
    """
    Delete the exam, optionally promoting or discarding its drafts.

    CONCURRENCY: row lock covers the mode guard AND the question
    handling. Two concurrent deletes now serialize: the loser blocks
    on the row lock, then `_locked_exam` returns None (the row is
    gone), which raises `EXAM_NOT_FOUND` → 404. Prior to this, both
    deletes ran their `draft_ids` computation and the through-table
    cleanup was idempotent only by accident — the second delete ran
    against already-deleted rows and its
    `_tag_and_publish_questions_to_bank` call (in DELETE_MODE_PUBLISH)
    would see no rows to update, returning an empty affected-author
    set and skipping a recompute the first delete had already done.

    A concurrent `publish_to_bank` and `delete` on the same exam now
    serialize: the loser's own guard trips
    (`CANNOT_DELETE_PUBLISHED_TO_BANK` for the delete, or the row is
    gone for the publish). The `can_be_deleted` check is re-evaluated
    under the lock, so a delete racing a `publish_to_bank` cannot
    slip past on stale `stored_status`.

    Returns None — the row no longer exists. The caller's pre-delete
    reference retains its old field values, which is what the view's
    success message uses.
    """
    if delete_mode not in VALID_DELETE_MODES:
        raise ValueError('INVALID_DELETE_MODE')

    affected_author_ids = []
    qids = []
    exam_name = None

    with transaction.atomic():
        locked = _locked_exam(exam.pk)

        if not locked.can_be_deleted:
            raise ValueError('CANNOT_DELETE_PUBLISHED_TO_BANK')

        qids = list(
            locked.exam_questions.values_list('question_id', flat=True)
        )
        exam_name = locked.name

        if delete_mode == DELETE_MODE_PUBLISH:
            # Same operation as publish_to_bank, minus the exam status
            # flip — the exam row is deleted immediately after.
            _, affected_author_ids = _tag_and_publish_questions_to_bank(
                locked, qids,
            )

        elif delete_mode == DELETE_MODE_DELETE:
            author_ids = {locked.primary_attending_id}
            author_ids.update(
                locked.co_attendings.values_list('id', flat=True)
            )
            drafts_to_delete = Question.objects.filter(
                id__in=qids,
                is_draft=True,
                draft_owner_id__in=author_ids,
            )
            draft_ids = list(drafts_to_delete.values_list('id', flat=True))
            # Capture authors who authored the drafts about to be
            # deleted. Their `authored_by` set shrinks, so their
            # counters must be recomputed too.
            affected_author_ids = list(
                drafts_to_delete
                .filter(authored_by_id__isnull=False)
                .values_list('authored_by_id', flat=True)
                .distinct()
            )
            if draft_ids:
                MasterExamQuestion.objects.filter(
                    question_id__in=draft_ids,
                ).delete()
            drafts_to_delete.delete()

        elif delete_mode == DELETE_MODE_KEEP:
            # Intentional no-op. The drafts are kept in the
            # question bank, detached from the exam. Their `is_draft`
            # flag is unchanged, so no author counter is affected.
            pass

        locked.delete()

    # Recompute any authors whose authored-question set changed as a
    # side effect of this delete. Bounded at one aggregate query plus
    # one bulk update regardless of how many authors are touched.
    if affected_author_ids:
        from apps.questions.services import QuestionService
        QuestionService._recompute_author_trust(affected_author_ids)

    if request is not None:
        log_privileged_action(
            request,
            action='master_exam.delete',
            target=None,
            target_repr=f'master_exam:{exam_name}',
            details={
                'delete_mode': delete_mode,
                'question_count': len(qids),
                'authors_recomputed': len(affected_author_ids),
            },
        )

    return None