# backend/apps/master_exams/services/master_exam_attempt_service/finish.py
"""
Grade and persist an attempt, plus the forced-finish helper.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone


from ...models import MasterExam, MasterExamAttempt
from .helpers import _grace_seconds, _resolve_weight

logger = logging.getLogger(__name__)


def finish(attempt, forced=False):

    if attempt.is_complete:
        return attempt
    return _finish_locked(attempt, forced)


def _finish_locked(attempt, forced):
    now = timezone.now()

    with transaction.atomic():
        fresh = (
            MasterExamAttempt.objects
            .select_for_update()
            .select_related('master_exam')
            .get(pk=attempt.pk)
        )
        if fresh.is_complete:
            return fresh

        is_forced = (
            forced
            or now > fresh.deadline_at + timedelta(seconds=_grace_seconds())
        )

        from apps.exams.services.exam_service import ExamService

        attempt_qids = list(fresh.question_ids or [])
        index_answers = {}
        for idx, qid in enumerate(attempt_qids):
            raw = fresh.answers.get(str(qid))
            if raw is None:
                continue
            index_answers[str(idx)] = {
                'answer': raw.get('answer'),
                'confidence': raw.get('confidence', True),
                'error_reason': raw.get('error_reason'),
            }

        result = ExamService.grade_exam(
            attempt_qids, index_answers, question_snapshots=fresh.grading_snapshot,
        )

        weight_map = {
            'easy': _resolve_weight(fresh.master_exam.weight_easy),
            'medium': _resolve_weight(fresh.master_exam.weight_medium),
            'hard': _resolve_weight(fresh.master_exam.weight_hard),
        }
        numerator = 0.0
        denominator = 0.0
        for r in result['questions']:
            w = weight_map.get(r.get('difficulty'), 1.0)
            denominator += w
            if r.get('is_correct'):
                numerator += w
        weighted_score = (
            round((numerator / denominator) * 100.0, 2)
            if denominator else 0.0
        )

        ExamService.record_completion_side_effects(fresh.user, result['questions'])

        fresh.results = result
        fresh.correct_count = result['correct_count']
        fresh.total_questions = result['total_questions']
        fresh.accuracy = result['accuracy']
        fresh.weighted_score = weighted_score
        fresh.finished_at = now
        fresh.is_complete = True
        fresh.forced_finish = is_forced
        fresh.save(update_fields=[
            'results', 'correct_count', 'total_questions',
            'accuracy', 'weighted_score', 'finished_at',
            'is_complete', 'forced_finish',
        ])

        return fresh


def _force_finish(attempt, reason='timeout'):
    try:
        finish(attempt, forced=True)
        logger.info(
            'Master exam attempt %s force-finished (%s)',
            attempt.id, reason,
        )
    except Exception:
        logger.exception(
            'Failed to force-finish master exam attempt %s', attempt.id,
        )
