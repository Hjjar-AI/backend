# backend/apps/users/apps.py

from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.users'
    verbose_name = 'Users'

    def ready(self):
        # Register the RoleCapabilities cache-invalidation signals.
        # The import is inside ready() so it runs after the app
        # registry is populated; a top-level import would fire the
        # signal receiver registration during app loading, which can
        # race with model import order.
        from . import signals  # noqa: F401