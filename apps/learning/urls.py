# backend/apps/learning/urls.py

from django.urls import path

from . import views

urlpatterns = [
    path('mistakes/', views.WrongAnswersView.as_view(), name='wrong-answers'),
    path('fragile/', views.FragileAnswersView.as_view(), name='fragile-answers'),
    path('attempt-summary/', views.AttemptSummaryView.as_view(), name='attempt-summary'),
    path('srs-due-count/', views.SRSDueCountView.as_view(), name='srs-due-count'),
    path('study-now/', views.StudyNowView.as_view(), name='study-now'),
    path('knowledge-map/', views.KnowledgeMapView.as_view(), name='knowledge-map'),
]
