# backend/apps/feedback/urls.py
from django.urls import path

from . import views

urlpatterns = [
    path('<int:question_id>/bookmark/', views.BookmarkToggleView.as_view(), name='bookmark-toggle'),
    path('<int:question_id>/flag/', views.FlagQuestionView.as_view(), name='flag-question'),
    path('<int:question_id>/rate/', views.RateQuestionView.as_view(), name='rate-question'),
    path('<int:question_id>/rating/', views.GetQuestionRatingView.as_view(), name='get-question-rating'),
    path('bookmarks/', views.BookmarkListView.as_view(), name='bookmark-list'),
    path('bookmarks/count/', views.BookmarkCountView.as_view(), name='bookmark-count'),

    # Batch ratings. Must not be captured by <int:question_id> above —
    # the int converter refuses a non-digit segment, so the literal
    # `ratings/` path is safe to place anywhere in the list.
    path('ratings/', views.QuestionRatingsBatchView.as_view(), name='ratings-batch'),

    path('admin/flags/', views.AdminFlagListView.as_view(), name='admin-flags'),
    path('admin/flags/<int:flag_id>/resolve/', views.AdminResolveFlagView.as_view(), name='admin-resolve-flag'),
]