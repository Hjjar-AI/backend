# backend/apps/groups/urls.py
from django.urls import path

from . import views

urlpatterns = [
    # ── Member-facing ────────────────────────────────────────────────
    path('study/groups/mine/', views.MyGroupsView.as_view(), name='my-groups'),
    path('study/groups/<int:group_id>/leaderboard/', views.GroupLeaderboardView.as_view(), name='group-leaderboard'),
    path('study/groups/<int:group_id>/visibility/', views.GroupVisibilityView.as_view(), name='group-visibility'),

    # ── Admin ────────────────────────────────────────────────────────
    path('study/admin/groups/', views.AdminGroupListView.as_view(), name='admin-group-list'),
    path('study/admin/groups/<int:group_id>/', views.AdminGroupDetailView.as_view(), name='admin-group-detail'),
    path('study/admin/groups/<int:group_id>/members/', views.AdminGroupMembersView.as_view(), name='admin-group-members'),
    path('study/admin/groups/<int:group_id>/members/<int:user_id>/', views.AdminGroupMemberView.as_view(), name='admin-group-member'),
]