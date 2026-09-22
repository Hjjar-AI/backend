# backend/apps/users/signals.py
"""
Cache-invalidation hooks for the capability system.

The role → capability map is cached per role in the Django cache
(see apps/users/services/permission_service.py). Any write to a
RoleCapabilities row must invalidate the cached copy, or role holders
will keep the old set until the cache entry expires.

The Vue panel calls invalidate_role_capabilities() explicitly, so
panel-driven edits are already correct. This signal extends the same
guarantee to every other write path:

  • Django admin edits at /admin/users/rolecapabilities/<id>/change/
  • manage.py shell / manage.py seed_capabilities
  • Raw ORM writes from anywhere

Without this signal, a direct edit silently leaves role holders with
stale capabilities for up to _CACHE_TTL seconds (currently 300).
"""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import RoleCapabilities
from .services.permission_service import invalidate_role_capabilities


@receiver(post_save, sender=RoleCapabilities, dispatch_uid='role_caps_save')
def _invalidate_on_save(sender, instance, **kwargs):
    invalidate_role_capabilities(instance.role)


@receiver(post_delete, sender=RoleCapabilities, dispatch_uid='role_caps_delete')
def _invalidate_on_delete(sender, instance, **kwargs):
    # A deleted row makes the role fall back to DEFAULT_ROLE_CAPABILITIES.
    # The cache must be dropped so the next resolution sees the fallback
    # rather than the previous (now-deleted) DB row.
    invalidate_role_capabilities(instance.role)