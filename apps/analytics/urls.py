# backend/apps/analytics/urls.py

from django.urls import path

from . import views

urlpatterns = [
    # ── Existing ────────────────────────────────────────────────────
    path('summary/', views.SummaryView.as_view(), name='analytics-summary'),
    path('active-users/', views.ActiveUsersStatsView.as_view(), name='active-users'),
    path(
        'verification-stats/',
        views.VerificationStatsView.as_view(),
        name='verification-stats',
    ),

    # ── Member-facing advanced (features 1, 3) ─────────────────────
    #
    # Both endpoints accept the caller's own id only — the view
    # ignores any query parameter and reads `request.user.id`, so
    # a member cannot request another user's mastery or streak.
    path(
        'category-mastery/',
        views.CategoryMasteryView.as_view(),
        name='analytics-category-mastery',
    ),
    path(
        'streak-history/',
        views.StreakHistoryView.as_view(),
        name='analytics-streak-history',
    ),

    # ── Admin advanced (features 4-8) ──────────────────────────────
    #
    # Every one of these requires 'analytics.view_all'. They are
    # separate routes (rather than one endpoint with a `report=`
    # query param) so each can be cached, throttled, and
    # capability-audited independently, and so the frontend can load
    # them lazily one at a time as the user expands each accordion.
    path(
        'admin/difficulty-calibration/',
        views.DifficultyCalibrationView.as_view(),
        name='analytics-difficulty-calibration',
    ),
    path(
        'admin/author-flag-rate/',
        views.AuthorFlagRateView.as_view(),
        name='analytics-author-flag-rate',
    ),
    path(
        'admin/exam-duration/',
        views.ExamDurationView.as_view(),
        name='analytics-exam-duration',
    ),
    path(
        'admin/cohort-comparison/',
        views.CohortComparisonView.as_view(),
        name='analytics-cohort-comparison',
    ),
    path(
        'admin/retention/',
        views.WeeklyRetentionView.as_view(),
        name='analytics-weekly-retention',
    ),
]