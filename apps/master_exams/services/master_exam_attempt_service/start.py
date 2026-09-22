# backend/apps/master_exams/services/master_exam_attempt_service/start.py
"""
Start (or resume) a master exam attempt, plus preview mode.
"""
import random
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.questions.models import Question
from apps.questions.payloads import build_grading_snapshot, exam_question_payload

from ...models import MasterExamAttempt
from .helpers import _grace_seconds


def _exam_question_ids(exam):
    """Ordered question ids of the exam, from the M2M relation."""
    return list(
        exam.exam_questions
        .order_by('order')
        .values_list('question_id', flat=True)
    )


def _shuffle_preserving_case_groups(question_ids):
    if not question_ids:
        return []
    rows = list(
        Question.objects
        .filter(id__in=question_ids)
        .values_list('id', 'case_id')
    )
    case_map = {qid: cid for qid, cid in rows}
    blocks = []
    seen_groups = set()
    for qid in question_ids:
        cid = case_map.get(qid)
        if cid is None:
            blocks.append([qid])
            continue
        if cid in seen_groups:
            continue
        seen_groups.add(cid)
        group_ids = [q for q in question_ids if case_map.get(q) == cid]
        blocks.append(group_ids)
    random.shuffle(blocks)
    flat = [qid for block in blocks for qid in block]
    return flat


def start(user, exam, is_preview=False):
    now = timezone.now()

    if is_preview:
        return _start_preview(user, exam)

    # ── Status gate (security) ─────────────────────────────────
    #
    # Only a published exam is startable. A draft has not been
    # released to participants; a cancelled exam is terminal; an
    # exam published to the bank is archived. The frontend hides
    # the Start button for these states, but the API must enforce
    # it too — anyone who can reach the exam (author, co-attending,
    # or audience member) is otherwise a single POST away from
    # starting a draft or cancelled exam.
    #
    # This check is deliberately AFTER the preview short-circuit:
    # previews exist precisely so the author can inspect a draft
    # before releasing it, so the status gate must not apply to
    # them.
    if exam.stored_status != 'published':
        raise ValueError('EXAM_NOT_STARTABLE')

    if now < exam.opens_at:
        raise ValueError('WINDOW_NOT_OPEN')

    is_makeup = False
    if now >= exam.closes_at:
        if not exam.allow_makeup:
            raise ValueError('WINDOW_CLOSED')
        is_makeup = True

    question_ids = _exam_question_ids(exam)
    if exam.shuffle_questions:
        question_ids = _shuffle_preserving_case_groups(question_ids)

    try:
        with transaction.atomic():
            if is_makeup:
                if MasterExamAttempt.objects.filter(
                    master_exam=exam, user=user, is_makeup=True,
                ).exists():
                    raise ValueError('MAKEUP_ALREADY_USED')
                if MasterExamAttempt.objects.filter(
                    master_exam=exam, user=user, is_makeup=False, is_complete=True,
                ).exists():
                    raise ValueError('ALREADY_ATTEMPTED')
            else:
                existing = MasterExamAttempt.objects.filter(
                    master_exam=exam, user=user, is_makeup=False,
                ).first()
                if existing is not None:
                    if existing.is_complete:
                        raise ValueError('ALREADY_ATTEMPTED')
                    return existing

            attempt = MasterExamAttempt.objects.create(
                master_exam=exam,
                user=user,
                session_id=str(uuid.uuid4()),
                question_ids=question_ids,
                grading_snapshot=build_grading_snapshot(question_ids),
                answers={},
                current_question_id=question_ids[0] if question_ids else None,
                started_at=now,
                deadline_at=now + timedelta(minutes=exam.duration_minutes),
                is_makeup=is_makeup,
                exam_name_snapshot=exam.name,
            )

        return attempt
    except IntegrityError:
        if is_makeup:
            existing = MasterExamAttempt.objects.filter(
                master_exam=exam, user=user, is_makeup=True,
            ).first()
            if existing is None:
                raise
            raise ValueError('MAKEUP_ALREADY_USED')
        existing = MasterExamAttempt.objects.filter(
            master_exam=exam, user=user, is_makeup=False,
        ).first()
        if existing is None:
            raise
        if existing.is_complete:
            raise ValueError('ALREADY_ATTEMPTED')
        return existing


def _start_preview(user, exam):
    question_ids = _exam_question_ids(exam)
    if exam.shuffle_questions:
        question_ids = _shuffle_preserving_case_groups(question_ids)

    questions_by_id = {
        q.id: q
        for q in Question.objects
        .filter(id__in=question_ids)
        .select_related('case')
    }

    payloads = []
    for qid in question_ids:
        q = questions_by_id.get(qid)
        if q is None:
            continue
        payloads.append({
            **exam_question_payload(q),
            'choices': q.choices or [],
            'correct_answer': q.correct_answer,
            'explanation': q.explanation or '',
        })

    return {
        'session_id': f'preview-{uuid.uuid4()}',
        'master_exam_id': exam.id,
        'exam_name_snapshot': exam.name,
        'question_ids': question_ids,
        'questions': payloads,
        'answers': {},
        'current_question_id': question_ids[0] if question_ids else None,
        'started_at': timezone.now(),
        'deadline_at': None,
        'is_preview': True,
        'is_makeup': False,
        'duration_minutes': exam.duration_minutes,
        'grace_seconds': _grace_seconds(),
    }
