# backend/apps/master_exams/services/master_exam_attempt_service/helpers.py
"""
Shared helpers for the attempt service modules.
"""
from django.conf import settings


def _grace_seconds():
    return getattr(settings, 'MASTER_EXAM_GRACE_SECONDS', 180)


def _last_answer_tolerance_seconds():
    return getattr(settings, 'MASTER_EXAM_LAST_ANSWER_TOLERANCE_SECONDS', 2)


def _resolve_weight(value):
    return 1.0 if value is None else float(value)