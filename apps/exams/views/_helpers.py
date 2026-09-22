# backend/apps/exams/views/_helpers.py
"""
Shared helper for the exam session views.
"""

from apps.core.models import Setting
from apps.core.runtime_settings import DEFAULT_RUNTIME_SETTINGS


def _exam_duration_minutes():
    setting = Setting.objects.filter(key='exam_duration_minutes').first()
    default = int(DEFAULT_RUNTIME_SETTINGS['exam_duration_minutes'])
    try:
        return int(setting.value) if setting else default
    except (TypeError, ValueError):
        return default
