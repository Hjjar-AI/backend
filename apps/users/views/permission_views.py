# backend/apps/users/views/permission_views.py

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView

from apps.core.permissions import HasCapability
from apps.core.utils import api_error, api_success
from apps.core.audit import log_privileged_action

from ..capabilities import (
    CAPABILITIES,
    CAPABILITY_GROUPS,
    DEFAULT_ROLE_CAPABILITIES,
)
from ..models import RoleCapabilities, User
from ..serializers import RoleCapabilitiesSerializer
from ..services.permission_service import (
    invalidate_role_capabilities,
    resolve_for_user,
    role_capabilities,
)


class _PermissionsAdminView(APIView):
    permission_classes = [HasCapability]
    required_capability = 'admin.permissions'


class RoleCapabilitiesListView(_PermissionsAdminView):
    """
    Read the full capability catalog and the current role → caps map,
    or replace one role's set.

    CONCURRENCY — `update_or_create`, NOT A HAND-ROLLED LOCK
    -------------------------------------------------------
    The `put` method writes one `RoleCapabilities` row per role and
    used to be an unlocked read-modify-write:

        row, created = RoleCapabilities.objects.get_or_create(
            role=role,
            defaults={'capabilities': caps},
        )
        if not created and row.capabilities != caps:
            row.capabilities = caps
            row.save(update_fields=['capabilities', 'updated_at'])

    Two concurrent PUTs on the same role could both read the same
    stale `row` and both write, with the loser's caps silently
    discarded. The window is narrow — this is an admin-only,
    human-speed action — but it is real, and a "grant this role
    capability X" request that lands second should not lose to one
    that landed first just because it happened to lose the CPU race.

    `update_or_create` is the correct primitive here. Django's
    implementation takes a `select_for_update()` on the existing-row
    path AND, on the create path, catches `IntegrityError` from a
    racing insert and falls through to the update loop — so the
    loser's caps are applied to the winner's freshly-created row
    rather than dropped. A hand-rolled `select_for_update` +
    `get_or_create` (an intermediate attempt) fixes the update
    branch but not the create branch; `update_or_create` is the one
    primitive that covers both.

    This is the same primitive `AdminSettingsView.post` uses for its
    `Setting` writes — one concurrency pattern for this shape across
    the codebase.

    BEHAVIOUR NOTE — NO-OP PUTs ADVANCE `updated_at`
    ------------------------------------------------
    `update_or_create` unconditionally calls `.save()` on the
    existing-row path, even when the incoming `caps` equal the
    stored value. The previous `if row.capabilities != caps:` guard
    skipped the save in that case. Net effect: a PUT whose payload
    matches the current state now bumps `updated_at` and fires the
    cache-invalidation signal registered in
    `apps/users/signals.py`.

    That is the correct trade — a caller who sends an identical
    payload has genuinely expressed the intent to set that state,
    and preserving a "skip the write if nothing changed" fast path
    would mean re-opening the read-modify-write window the fix
    exists to close. The `updated_at` bump on a no-op PUT is the
    honest audit trail.
    """

    def get(self, request):
        rows = {
            row.role: list(row.capabilities or [])
            for row in RoleCapabilities.objects.all()
        }

        merged = {}
        for role, defaults in DEFAULT_ROLE_CAPABILITIES.items():
            if role in rows:
                merged[role] = rows[role]
            else:
                merged[role] = sorted(defaults)

        return api_success(data={
            'capabilities': sorted(CAPABILITIES),
            'groups': [
                {'label': label, 'capabilities': list(caps)}
                for label, caps in CAPABILITY_GROUPS
            ],
            'roles': merged,
        })

    def put(self, request):
        serializer = RoleCapabilitiesSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        role = serializer.validated_data['role']
        caps = serializer.validated_data['capabilities']

        if role == 'admin':
            return api_error(
                'لا يمكن تعديل صلاحيات المدير — المدير يمتلك كل الصلاحيات دائماً',
                400,
            )

        # See the class docstring for the full rationale on why this
        # is `update_or_create` and not a hand-rolled lock. The
        # surrounding `transaction.atomic()` is not strictly required
        # — `update_or_create` opens its own — but keeping it makes
        # the intent explicit ("this is one atomic write") and
        # matches the pattern `AdminSettingsView.post` already uses
        # for the same shape.
        with transaction.atomic():
            row, created = RoleCapabilities.objects.update_or_create(
                role=role,
                defaults={'capabilities': caps},
            )

        # Cache invalidation runs AFTER the transaction commits: a
        # rollback leaves the cache coherent. The signal in
        # apps/users/signals.py is the backstop for every other write
        # path (Django admin, management commands, raw ORM); this
        # explicit call is the panel's guarantee that a
        # just-committed change is visible on the very next request.
        invalidate_role_capabilities(role)

        log_privileged_action(
            request,
            action='permissions.role_update',
            target=row,
            target_repr=role,
            details={
                'capability_count': len(caps),
                'created': created,
            },
        )

        return api_success(
            data={'role': role, 'capabilities': caps},
            message='تم تحديث صلاحيات الدور',
        )


class UserCapabilitiesView(_PermissionsAdminView):
    """
    Read or replace one user's capability overrides.

    Stub users are rejected. A stub is an author record created by the
    import flow to represent an external contributor with no local
    account. It cannot log in and has no meaningful permission set.
    The 404 from the filtered `get_object_or_404` is the same
    response a nonexistent user id would produce, so stub existence
    is not enumerable through this endpoint.
    """

    def get(self, request, user_id):
        user = get_object_or_404(User, id=user_id, is_stub=False)
        return api_success(data={
            'user_id': user.id,
            'username': user.username,
            'role': user.role,
            'role_capabilities': sorted(role_capabilities(user.role)),
            'overrides': user.capabilities or {},
            'resolved': sorted(resolve_for_user(user)),
        })

    def put(self, request, user_id):
        user = get_object_or_404(User, id=user_id, is_stub=False)

        raw = request.data.get('capabilities')
        if not isinstance(raw, dict):
            return api_error('capabilities must be an object', 400)

        unknown = set(raw) - CAPABILITIES
        if unknown:
            return api_error(
                f'Unknown capabilities: {sorted(unknown)}',
                400,
            )

        cleaned = {}
        for cap, value in raw.items():
            if value is None:
                continue
            if not isinstance(value, bool):
                return api_error(f'{cap} must be true, false, or null', 400)
            cleaned[cap] = value

        user.capabilities = cleaned
        user.save(update_fields=['capabilities'])

        if hasattr(user, '_resolved_caps_cache'):
            delattr(user, '_resolved_caps_cache')

        log_privileged_action(
            request,
            action='permissions.user_overrides_update',
            target=user,
            target_repr=user.username,
            details={
                'override_count': len(cleaned),
                'overrides': cleaned,
            },
        )

        return api_success(
            data={
                'user_id': user.id,
                'overrides': cleaned,
                'resolved': sorted(resolve_for_user(user)),
            },
            message='تم تحديث صلاحيات المستخدم',
        )