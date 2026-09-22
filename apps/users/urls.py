# backend/apps/users/urls.py

from django.urls import path

from . import views

urlpatterns = [
    # ── Auth ──────────────────────────────────────────────────────────
    path('csrf/', views.GetCSRFTokenView.as_view(), name='csrf-token'),
    path('login/', views.LoginView.as_view(), name='login'),
    path('logout/', views.LogoutView.as_view(), name='logout'),
    path('me/', views.MeView.as_view(), name='me'),
    path(
        'change-password/',
        views.ChangePasswordView.as_view(),
        name='change-password',
    ),

    # ── Admin: users ──────────────────────────────────────────────────
    path(
        'admin/users/',
        views.AdminUserListView.as_view(),
        name='admin-users-list',
    ),
    path(
        'admin/users/<int:user_id>/',
        views.AdminUserDetailView.as_view(),
        name='admin-user-detail',
    ),
    path(
        'admin/users/<int:user_id>/toggle/',
        views.AdminToggleUserView.as_view(),
        name='admin-user-toggle',
    ),
    path(
        'admin/users/<int:user_id>/reset-password/',
        views.AdminResetPasswordView.as_view(),
        name='admin-user-reset-password',
    ),
    path(
        'admin/active-users/',
        views.AdminActiveUsersView.as_view(),
        name='admin-active-users',
    ),

    # ── Admin: capability panel ───────────────────────────────────────
    path(
        'admin/permissions/',
        views.RoleCapabilitiesListView.as_view(),
        name='admin-permissions-roles',
    ),
    path(
        'admin/permissions/users/<int:user_id>/',
        views.UserCapabilitiesView.as_view(),
        name='admin-permissions-user',
    ),
]