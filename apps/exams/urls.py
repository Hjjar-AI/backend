from django.urls import path
from . import views

urlpatterns = [
    path('start/<str:mode>/', views.StartSessionView.as_view(), name='start-session'),
    path('question/', views.GetQuestionView.as_view(), name='get-question'),
    path('answer/', views.SubmitAnswerView.as_view(), name='submit-answer'),
    path('results/', views.FinishSessionView.as_view(), name='finish-session'),
    path('pause/', views.PauseSessionView.as_view(), name='pause-session'),
    path('resume/', views.ResumeSessionView.as_view(), name='resume-session'),
    path('discard/', views.DiscardProgressView.as_view(), name='discard-progress'),
    path('status/', views.StatusView.as_view(), name='status'),
    path('history/', views.TestHistoryView.as_view(), name='test-history'),

    path('blueprints/', views.BlueprintListView.as_view(), name='blueprint-list'),
    path('blueprints/<int:blueprint_id>/', views.BlueprintDetailView.as_view(), name='blueprint-detail'),
]