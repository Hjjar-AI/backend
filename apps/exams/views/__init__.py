# backend/apps/exams/views/__init__.py
"""
Package surface for the exam views.

`apps/exams/urls.py` does `from . import views` and references every
view class through the module namespace. `config/urls.py` imports
`TestHistoryView` directly. Both import paths keep working after the
split from a single `views.py` module into this package, because
every public view class is re-exported here.
"""

from .session_views import (
    StartSessionView,
    GetQuestionView,
    SubmitAnswerView,
    FinishSessionView,
    PauseSessionView,
    ResumeSessionView,
    DiscardProgressView,
    StatusView,
)
from .history_views import TestHistoryView
from .blueprint_views import BlueprintListView, BlueprintDetailView


__all__ = [
    # Session runner
    'StartSessionView',
    'GetQuestionView',
    'SubmitAnswerView',
    'FinishSessionView',
    'PauseSessionView',
    'ResumeSessionView',
    'DiscardProgressView',
    'StatusView',
    # History
    'TestHistoryView',
    # Blueprints
    'BlueprintListView',
    'BlueprintDetailView',
]