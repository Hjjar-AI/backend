# backend/apps/master_exams/services/master_exam_attempt_service/status.py
"""
Attempt status payload for the polling view.
"""
from django.utils import timezone

from .helpers import _grace_seconds


def status(attempt):
    attempt_qids = list(attempt.question_ids or [])
    return {
        'attempt_id': attempt.id,
        'session_id': attempt.session_id,
        'master_exam_id': attempt.master_exam_id,
        'is_active': not attempt.is_complete,
        'is_complete': attempt.is_complete,
        'is_makeup': attempt.is_makeup,
        'question_ids': attempt_qids,
        'answers': attempt.answers,
        'current_question_id': attempt.current_question_id,
        'started_at': attempt.started_at.isoformat(),
        'deadline_at': attempt.deadline_at.isoformat(),
        'duration_minutes': attempt.master_exam.duration_minutes,
        'grace_seconds': _grace_seconds(),
        'server_now': timezone.now().isoformat(),
    }