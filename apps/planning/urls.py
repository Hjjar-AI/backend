# backend/apps/planning/urls.py
from django.urls import path

from . import views

urlpatterns = [
    path('study-planner/', views.GetPlannerView.as_view(), name='get-planner'),
    path('study-planner/update/', views.UpdatePlannerView.as_view(), name='update-planner'),
    path('study-planner/progress/', views.RecordProgressView.as_view(), name='record-progress'),
    path('study-planner/delete/', views.DeletePlannerView.as_view(), name='delete-planner'),

    # Streak and heatmap are personal study-state reads, not planner
    # configuration — but they belong to the planning/learning domain,
    # not to groups, so they live here.
    path('study/streak/', views.MyStreakView.as_view(), name='my-streak'),
    path('study/activity-heatmap/', views.ActivityHeatmapView.as_view(), name='activity-heatmap'),
]