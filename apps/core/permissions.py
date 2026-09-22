# backend/apps/core/permissions.py

from django.core.exceptions import ImproperlyConfigured
from rest_framework import permissions


# NOTE: the `IsAdmin` class was removed here. It had been marked
# deprecated with its own removal condition ("Remove once
# `grep -r 'IsAdmin' apps/` returns nothing outside this file"), and
# that condition was met: the class was imported by zero views.
# Every gated view uses `HasCapability` instead. See the capability
# constants in `apps/users/capabilities.py` for the canonical set.


class IsMasterExamParticipant(permissions.BasePermission):
    """
    Any authenticated user. The participant check is done at the
    view level because it depends on the specific exam's audience,
    which is object-level rather than capability-level.
    """
    message = 'يجب تسجيل الدخول.'

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)


class HasCapability(permissions.BasePermission):
    """
    Grant access when the caller holds the capability named by
    `view.required_capability`.

    FAIL-CLOSED CONFIGURATION
    -------------------------
    A view that uses `HasCapability` MUST declare `required_capability`.
    If it does not, this permission class raises
    `ImproperlyConfigured` at request time rather than silently
    allowing every authenticated user through.

    The previous behavior was `if not cap: return True`. That meant a
    new view that forgot the attribute would ship with no capability
    gate at all, and the omission would only be noticed if someone
    audited every view's permission classes. Failing closed turns
    the omission into a 500 on first request, which is exactly the
    kind of signal a misconfiguration should produce in development.

    If a view intends "any authenticated user", it should use DRF's
    `IsAuthenticated` — using `HasCapability` with no capability is
    a category error (declaring intent to gate but not gating).

    PHASE 5 — the `is_admin` fast path that lived here during the
    migration has been removed. Every view is now responsible for
    declaring its capability, and the check is uniform for every
    user, including admins. An admin holds every capability by
    virtue of the resolve_for_user() short-circuit in
    apps/users/services/permission_service.py, so the fast path was
    redundant. Removing it means a typo'd capability on a view now
    fails loudly for everyone instead of being silently bypassed for
    admins.
    """
    message = 'ليس لديك صلاحية لهذا الإجراء.'

    def has_permission(self, request, view):
        u = getattr(request, 'user', None)
        if not u or not u.is_authenticated:
            return False

        cap = getattr(view, 'required_capability', None)
        if not cap:
            # Fail closed at configuration time. A view that reaches
            # this branch is a developer error — see the class
            # docstring for the full rationale.
            raise ImproperlyConfigured(
                f'{view.__class__.__name__} uses HasCapability but does '
                f'not declare `required_capability`. Either declare a '
                f'capability, or use `IsAuthenticated` if any '
                f'authenticated user is intended to pass.'
            )

        return u.has_capability(cap)