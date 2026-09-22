# backend/apps/users/admin.py

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db.models import Q

from .models import User, ActiveSession, LoginAttempt, RoleCapabilities


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = (
        'username', 'full_name', 'role', 'is_active', 'is_stub',
        'trust_score', 'expires_at',
    )
    list_filter = ('role', 'is_active', 'is_staff', 'is_stub')
    search_fields = ('username', 'full_name', 'email')
    ordering = ('-created_at',)

    # Maintenance note on `fieldsets`:
    #
    # Every field name listed below must exist on the User model.
    # Adding or removing a model field requires updating this tuple
    # in the same commit. A stale entry does not fail at import time
    # — it fails the first time an admin opens a user edit page,
    # with a FieldError raised deep inside Django's admin render.
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Personal info', {'fields': ('full_name', 'email')}),
        ('Identity', {
            'fields': ('uuid',),
            'description': (
                'Stable cross-system identifier. Read-only — set once '
                'at creation and referenced by the state envelope.'
            ),
        }),
        ('Permissions', {
            'fields': (
                'is_active', 'is_staff', 'is_superuser',
                'groups', 'user_permissions',
            ),
        }),
        ('Role & Subscription', {
            'fields': ('role', 'expires_at', 'auto_renew_days'),
        }),
        ('Stub', {
            'fields': ('is_stub',),
            'description': (
                'Set True by the import flow for external authors '
                'with no local account. Stubs cannot log in and are '
                'excluded from user listings. To promote one to a '
                'real user: clear this flag, set a usable password, '
                'and set is_active=True.'
            ),
        }),
        ('Capability overrides', {
            'fields': ('capabilities',),
            'description': (
                'Per-user capability overrides. Keys are capability '
                'strings; values are true/false. Absent keys inherit '
                'the role default. Editing here does not invalidate '
                'the in-process capability cache — restart the server '
                'or run `manage.py seed --only capabilities` afterward '
                'if the change should take effect immediately.'
            ),
        }),
        ('Stats', {
            'fields': (
                'questions_count',
                'trust_score',
                'must_change_password',
            ),
        }),
        ('Important dates', {'fields': ('last_login', 'created_at')}),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': (
                'username', 'password1', 'password2',
                'role', 'full_name',
            ),
        }),
    )

    readonly_fields = ('uuid',)

    def has_delete_permission(self, request, obj=None):
        """
        Refuse deletion of any user who authored or owns a question.

        The FK uses PROTECT, so a raw ORM delete would raise
        ProtectedError. Blocking here gives the operator a clear
        message in the Django admin instead of a 500. Use
        `User.deactivate()` instead — the account is disabled but its
        authorship survives.
        """
        if obj is None:
            return True
        from apps.questions.models import Question
        if Question.objects.filter(
            Q(authored_by=obj) | Q(owned_by=obj)
        ).exists():
            return False
        return True


@admin.register(ActiveSession)
class ActiveSessionAdmin(admin.ModelAdmin):
    list_display = ('session_id', 'user', 'ip', 'last_seen')
    search_fields = ('session_id', 'user__username', 'ip')


@admin.register(LoginAttempt)
class LoginAttemptAdmin(admin.ModelAdmin):
    list_display = ('username', 'ip', 'success', 'attempt_time')
    list_filter = ('success',)
    search_fields = ('username', 'ip')


@admin.register(RoleCapabilities)
class RoleCapabilitiesAdmin(admin.ModelAdmin):
    """
    Django-admin fallback editor for role → capability mappings.

    This is a fallback. The Vue panel at /admin/permissions is the
    intended editor and it invalidates the capability cache
    automatically. Editing here does not.

    The 'admin' row is present but inert — resolve_for_user()
    short-circuits the admin role to the full CAPABILITIES set and
    never consults this row. Do not delete it; keeping it visible
    makes the panel's read-back consistent.
    """
    list_display = ('role', 'capability_count', 'updated_at')
    readonly_fields = ('updated_at',)

    def capability_count(self, obj):
        return len(obj.capabilities or [])
    capability_count.short_description = 'Capabilities'

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False