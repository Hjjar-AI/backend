# backend/apps/master_exams/urls.py
from django.urls import path

from . import views

urlpatterns = [
    # ── CRUD ──────────────────────────────────────────────────────────
    path('', views.MasterExamListCreateView.as_view(), name='master-exam-list-create'),
    path('drafts/', views.MasterExamDraftsLibraryView.as_view(), name='master-exam-drafts'),
    path('drafts/<int:draft_id>/', views.MasterExamDraftDetailView.as_view(), name='master-exam-draft-detail'),
    path('needs-acknowledgement/', views.MasterExamNeedsAcknowledgementView.as_view(), name='master-exam-needs-ack'),
    path('<int:pk>/', views.MasterExamDetailView.as_view(), name='master-exam-detail'),
    path('<int:pk>/acknowledge/', views.MasterExamAcknowledgeView.as_view(), name='master-exam-acknowledge'),

    # ── Composition ───────────────────────────────────────────────────
    path('<int:pk>/questions/add/', views.MasterExamAddQuestionsView.as_view(), name='master-exam-add-questions'),
    path('<int:pk>/questions/remove/', views.MasterExamRemoveQuestionView.as_view(), name='master-exam-remove-question'),
    path('<int:pk>/questions/reorder/', views.MasterExamReorderView.as_view(), name='master-exam-reorder'),
    path('<int:pk>/drafts/', views.MasterExamAddDraftView.as_view(), name='master-exam-add-draft'),

    # ── Lifecycle ─────────────────────────────────────────────────────
    path('<int:pk>/publish/', views.MasterExamPublishView.as_view(), name='master-exam-publish'),
    path('<int:pk>/cancel/', views.MasterExamCancelView.as_view(), name='master-exam-cancel'),
    path('<int:pk>/publish-to-bank/', views.MasterExamPublishToBankView.as_view(), name='master-exam-publish-to-bank'),

    # ── Attempt ───────────────────────────────────────────────────────
    path('<int:pk>/start/', views.MasterExamStartAttemptView.as_view(), name='master-exam-start'),
    path('<int:pk>/attempt/status/', views.MasterExamAttemptStatusView.as_view(), name='master-exam-attempt-status'),
    path('<int:pk>/attempt/question/', views.MasterExamCurrentQuestionView.as_view(), name='master-exam-attempt-question'),
    path('<int:pk>/attempt/answer/', views.MasterExamSubmitAnswerView.as_view(), name='master-exam-attempt-answer'),
    path('<int:pk>/attempt/goto/', views.MasterExamGotoView.as_view(), name='master-exam-attempt-goto'),
    path('<int:pk>/attempt/finish/', views.MasterExamFinishAttemptView.as_view(), name='master-exam-attempt-finish'),
    path('<int:pk>/attempt/flag/', views.MasterExamFlagView.as_view(), name='master-exam-attempt-flag'),

    # ── Results ───────────────────────────────────────────────────────
    path('<int:pk>/results/', views.MasterExamResultsView.as_view(), name='master-exam-results'),
    path('<int:pk>/results/summary.csv/', views.MasterExamResultsSummaryCSVView.as_view(), name='master-exam-results-summary-csv'),
    path('<int:pk>/results/matrix.csv/', views.MasterExamResultsMatrixCSVView.as_view(), name='master-exam-results-matrix-csv'),
]