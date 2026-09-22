# backend/apps/users/views/__init__.py
"""
Package surface for the user views.

`apps/users/urls.py` does `from . import views` and then references
every view class through the module namespace. To keep that import
path working unchanged after the split from a single `views.py`
module into this package, every public view class is re-exported here.
"""
from .auth_views import (
    LoginView,
    LogoutView,
    MeView,
    ChangePasswordView,
    GetCSRFTokenView,
)
from .admin_user_views import (
    AdminUserListView,
    AdminUserDetailView,
    AdminToggleUserView,
    AdminResetPasswordView,
    AdminActiveUsersView,
)
from .permission_views import (
    RoleCapabilitiesListView,
    UserCapabilitiesView,
)

__all__ = [
    # Auth
    'LoginView',
    'LogoutView',
    'MeView',
    'ChangePasswordView',
    'GetCSRFTokenView',
    # Admin user management
    'AdminUserListView',
    'AdminUserDetailView',
    'AdminToggleUserView',
    'AdminResetPasswordView',
    'AdminActiveUsersView',
    # Capability panel
    'RoleCapabilitiesListView',
    'UserCapabilitiesView',
]