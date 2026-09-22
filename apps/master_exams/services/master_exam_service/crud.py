# backend/apps/master_exams/services/master_exam_service/crud.py
"""
Create / update for MasterExam.

`create` also populates the ordered question relation, the audience
M2M, and the co-attending M2M. `update` is the optimistic-locking
path: it bumps `version` and rejects a stale `expected_version`.

AUDIENCE SETTERS (fix — shared between create and update)
---------------------------------------------------------
The three M2M setters (`audience_groups`, `audience_users`,
`co_attendings`) were inlined in both `create` and `update`,
identically. `create` applied them unconditionally (its input is a
fully-populated `validated_data`); `update` gated them on `X in
data` so a partial edit that did not mention a relation would not
clear it. That behavioural difference was preserved by making the
gate live inside the helper — a data dict that contains none of the
relation keys is a no-op, a dict that contains one of them sets
exactly that relation and leaves the others alone.
"""
import logging

from django.db import transaction
from django.db.models import F

from apps.questions.models import Question
from apps.groups.models import Group
from apps.users.models import User
from apps.core.audit import log_privileged_action

from ...models import (
    MasterExam,
    MasterExamQuestion,
)

logger = logging.getLogger(__name__)


def _apply_audience(exam, data, primary_attending_id):
    """
    Apply the three audience/co-attending relations.

    Each setter fires only when its key is present in `data`. For
    `create`, `data` comes from the fully-validated serializer and
    always contains all three keys (with empty lists where the
    caller sent nothing), so the helper behaves like the previous
    unconditional `create` block. For `update`, `data` is the
    filtered dict built by `MasterExamDetailView.put` (keys the
    caller actually sent), so a partial edit that omits a relation
    leaves it untouched — matching the previous `update` block.

    The `exclude(id=primary_attending_id)` is what keeps the author
    of record out of their own audience. It was duplicated across
    both call sites previously; it is now the single place it can
    be forgotten.
    """
    if 'audience_group_ids' in data:
        group_ids = data.get('audience_group_ids') or []
        exam.audience_groups.set(Group.objects.filter(id__in=group_ids))

    if 'audience_user_ids' in data:
        user_ids = data.get('audience_user_ids') or []
        exam.audience_users.set(
            User.objects
            .filter(id__in=user_ids)
            .exclude(id=primary_attending_id)
        )

    if 'co_attending_ids' in data:
        co_ids = data.get('co_attending_ids') or []
        exam.co_attendings.set(
            User.objects
            .filter(id__in=co_ids)
            .exclude(id=primary_attending_id)
        )


def create(primary_attending, data, request=None):
    raw_qids = list(data.get('question_ids') or [])
    if raw_qids:
        visible_ids = set(
            Question.objects
            .filter(id__in=raw_qids)
            .visible_to(primary_attending)
            .values_list('id', flat=True)
        )
        missing = [qid for qid in raw_qids if qid not in visible_ids]
        if missing:
            raise ValueError(f'QUESTIONS_NOT_ACCESSIBLE:{missing}')

    with transaction.atomic():
        exam = MasterExam.objects.create(
            name=data['name'],
            description=data.get('description') or None,
            instructions=data.get('instructions') or None,
            primary_attending=primary_attending,
            opens_at=data['opens_at'],
            closes_at=data['closes_at'],
            duration_minutes=data['duration_minutes'],
            audience_all_doctors=bool(data.get('audience_all_doctors', False)),
            weight_easy=float(data.get('weight_easy', 1.0)),
            weight_medium=float(data.get('weight_medium', 1.0)),
            weight_hard=float(data.get('weight_hard', 1.0)),
            shuffle_questions=bool(data.get('shuffle_questions', True)),
            shuffle_choices=bool(data.get('shuffle_choices', False)),
            allow_makeup=bool(data.get('allow_makeup', True)),
            exam_topic_tag=(data.get('exam_topic_tag') or '').strip() or None,
        )

        if raw_qids:
            MasterExamQuestion.objects.bulk_create([
                MasterExamQuestion(
                    master_exam=exam,
                    question_id=qid,
                    order=i + 1,
                )
                for i, qid in enumerate(raw_qids)
            ])

        _apply_audience(exam, data, primary_attending.id)

    if request is not None:
        log_privileged_action(
            request,
            action='master_exam.create',
            target=exam,
            target_repr=exam.name,
            details={
                'opens_at': exam.opens_at.isoformat(),
                'closes_at': exam.closes_at.isoformat(),
                'duration_minutes': exam.duration_minutes,
                'question_count': len(raw_qids),
            },
        )

    return exam


def update(exam, data, expected_version=None, request=None):
    """
    Update a MasterExam.

    OPTIMISTIC LOCKING
    ------------------
    Every write path here — scalar fields, audience groups, audience
    users, co-attendings — advances `version` by exactly one and
    rejects a stale `expected_version` with `MODIFIED_BY_ANOTHER_USER`.
    An empty update only checks the expected version; it does not
    mutate the exam.

    The previous revision split the branches: scalar-only updates used
    `update(version=F('version') + 1)`, but the M2M-only branch used
    `update(version=F('version'))`, a set-to-self no-op. The effect
    was that a request that only replaced the audience set left the
    version unchanged — two administrators editing M2M relations in
    sequence both passed the version check and the second silently
    overwrote the first, with no conflict response to the loser.

    The M2M branch now advances the version too. The order of
    operations is unchanged: the version CAS runs first, and only
    then are the M2M relations replaced. A caller that supplies a
    stale version is rejected before any relation is touched.
    """
    if not exam.can_edit_now:
        raise ValueError('EXAM_WINDOW_STARTED')

    if expected_version is None:
        raise ValueError('MISSING_EXPECTED_VERSION')
    expected_version = int(expected_version)

    with transaction.atomic():
        update_fields = {}
        for field in (
            'name', 'description', 'instructions',
            'opens_at', 'closes_at', 'duration_minutes',
            'audience_all_doctors',
            'weight_easy', 'weight_medium', 'weight_hard',
            'shuffle_questions', 'shuffle_choices',
            'allow_makeup', 'exam_topic_tag',
        ):
            if field in data:
                update_fields[field] = data[field]

        m2m_fields = {
            'audience_group_ids', 'audience_user_ids', 'co_attending_ids',
        }
        version_match = MasterExam.objects.filter(
            pk=exam.pk,
            version=expected_version,
        )

        if update_fields or m2m_fields.intersection(data):
            update_fields['version'] = F('version') + 1
            if version_match.update(**update_fields) == 0:
                raise ValueError('MODIFIED_BY_ANOTHER_USER')
            exam.refresh_from_db()
        elif not version_match.exists():
            raise ValueError('MODIFIED_BY_ANOTHER_USER')

        _apply_audience(exam, data, exam.primary_attending_id)

    if request is not None:
        log_privileged_action(
            request,
            action='master_exam.update',
            target=exam,
            target_repr=exam.name,
            details={'fields': sorted(data.keys())},
        )

    return exam