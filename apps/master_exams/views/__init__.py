# backend/apps/master_exams/views/__init__.py
"""
Package surface for the master-exam views.
SPLIT 1.1: moved from apps/exams/views_master/ into apps/master_exams/views/.
`urls.py` does `from . import views` and references every view class through
the module namespace.
"""
from .crud_views import (
    MasterExamListCreateView,
    MasterExamDetailView,
    MasterExamAcknowledgeView,
    MasterExamNeedsAcknowledgementView,
)
from .composition_views import (
    MasterExamAddQuestionsView,
    MasterExamRemoveQuestionView,
    MasterExamReorderView,
    MasterExamAddDraftView,
)
from .lifecycle_views import (
    MasterExamPublishView,
    MasterExamCancelView,
    MasterExamPublishToBankView,
)
from .attempt_views import (
    MasterExamStartAttemptView,
    MasterExamAttemptStatusView,
    MasterExamCurrentQuestionView,
    MasterExamSubmitAnswerView,
    MasterExamGotoView,
    MasterExamFinishAttemptView,
    MasterExamFlagView,
)
from .results_views import (
    MasterExamResultsView,
    MasterExamResultsSummaryCSVView,
    MasterExamResultsMatrixCSVView,
)
from .drafts_views import (
    MasterExamDraftsLibraryView,
    MasterExamDraftDetailView,
)

__all__ = [
    'MasterExamListCreateView',
    'MasterExamDetailView',
    'MasterExamAcknowledgeView',
    'MasterExamNeedsAcknowledgementView',
    'MasterExamAddQuestionsView',
    'MasterExamRemoveQuestionView',
    'MasterExamReorderView',
    'MasterExamAddDraftView',
    'MasterExamPublishView',
    'MasterExamCancelView',
    'MasterExamPublishToBankView',
    'MasterExamStartAttemptView',
    'MasterExamAttemptStatusView',
    'MasterExamCurrentQuestionView',
    'MasterExamSubmitAnswerView',
    'MasterExamGotoView',
    'MasterExamFinishAttemptView',
    'MasterExamFlagView',
    'MasterExamResultsView',
    'MasterExamResultsSummaryCSVView',
    'MasterExamResultsMatrixCSVView',
    'MasterExamDraftsLibraryView',
    'MasterExamDraftDetailView',
]