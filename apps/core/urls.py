# backend/apps/core/urls.py

from django.urls import path

from . import views

# NOTE ON THE IMPORT BELOW
# ------------------------
# `RefreshAuthorRanksView` used to live in `apps.core.views`. It was
# moved to `apps.questions.views.reputation_views` (see that module's
# docstring for the rationale — a `core` app reaching into a feature
# app was the wrong dependency direction). The URL is registered here
# rather than in `questions/urls.py` to preserve the existing
# `/api/v1/admin/refresh-author-ranks/` wire path without a frontend
# coordination. URLs are configuration, and a config-level import of a
# feature-app view is not the same architectural smell as a
# config-level *implementation* of one.
from apps.questions.views import RefreshAuthorRanksView


urlpatterns = [
    path('health/', views.HealthView.as_view(), name='health'),
    path('config/', views.PublicConfigView.as_view(), name='public-config'),
    path('tips/', views.TipsView.as_view(), name='tips'),
    path('admin/settings/', views.AdminSettingsView.as_view(), name='admin-settings'),
    path(
        'admin/refresh-author-ranks/',
        RefreshAuthorRanksView.as_view(),
        name='admin-refresh-author-ranks',
    ),
    path(
        'admin/seed-sample-questions/',
        views.SeedSampleQuestionsView.as_view(),
        name='admin-seed-sample-questions',
    ),
]