# backend/apps/users/models.py

from datetime import timedelta
import re
import uuid

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import MinLengthValidator, RegexValidator
from django.db import models
from django.utils import timezone


USERNAME_REGEX = re.compile(r'^[a-zA-Z0-9_\u0600-\u06FF]{3,50}$')


class UserManager(BaseUserManager):
    def create_user(self, username, password=None, **extra_fields):
        if not username:
            raise ValueError('اسم المستخدم مطلوب')
        # The regex check lives here as a hard runtime guard for
        # callers that bypass the serializer (management commands,
        # shell scripts, seed data). The serializer and the field
        # validator enforce the same rule at the API boundary; this
        # is the last line of defense before the row hits the DB.
        if not USERNAME_REGEX.match(username):
            raise ValueError('اسم المستخدم يجب أن يحتوي على أحرف وأرقام فقط (3-50 حرفاً)')
        user = self.model(username=username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('role', 'admin')
        return self.create_user(username, password, **extra_fields)

    def create_stub(self, username):
        """
        Create (or return) a stub user representing an external author
        whose questions were imported but who has no local account.

        Stubs are:
          • is_stub=True       — excluded from every user picker and
                                 list, and rejected at login.
          • is_active=False    — cannot authenticate even if the
                                 password check were bypassed.
          • unusable password  — `make_password(None)` produces a
                                 hash that `check_password()` can
                                 never match. Set here in the
                                 manager so the stub is created in
                                 a single INSERT.

        Idempotent by username — returns the existing row if one is
        already present, so repeated imports of the same envelope do
        not create duplicate stubs.
        """
        stub, _created = self.get_or_create(
            username=username,
            defaults={
                'is_stub': True,
                'is_active': False,
                'password': make_password(None),
                'role': 'member',
            },
        )
        return stub


class User(AbstractBaseUser, PermissionsMixin):
    ROLE_CHOICES = [
        ('admin',     'Admin'),
        ('moderator', 'Moderator'),
        ('member',    'Member'),
    ]

    # ── Portable identity ─────────────────────────────────────────────
    #
    # Stable, globally unique. Used by the state envelope so a
    # question imported from another installation can be re-imported
    # idempotently, and so a user's identity survives a rename. The
    # local integer id remains the primary key; this is a parallel
    # identifier for cross-system references only.
    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
    )

    username = models.CharField(
        max_length=50,
        unique=True,
        validators=[
            MinLengthValidator(3),
            # Single source of truth for the accepted character set.
            # `max_length=50` above already handles the upper bound,
            # and MinLengthValidator handles the lower bound; the
            # regex's `{3,50}` quantifier is redundant but keeps the
            # rule self-contained if the field length is ever changed.
            RegexValidator(
                regex=USERNAME_REGEX,
                message='اسم المستخدم يجب أن يحتوي على أحرف وأرقام فقط (3-50 حرفاً)',
            ),
        ],
    )
    full_name = models.CharField(max_length=100, blank=True, null=True)
    email = models.EmailField(max_length=100, blank=True, null=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='member')
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    # ── Stub marker ───────────────────────────────────────────────────
    #
    # True for users created automatically by the import flow to
    # represent an external author whose name appeared in an envelope
    # but did not match any local account. Stubs:
    #
    #   • Cannot log in (AuthenticationService.login_user rejects
    #     them before the password check).
    #   • Are excluded from every user listing, picker, and count.
    #   • Accumulate a trust_score of 0 and are excluded from the
    #     leaderboard by the same filter.
    #
    # If the real person later joins the system, an admin clears
    # `is_stub` and sets a password on the existing row — their
    # already-imported questions are then attributed to them.
    is_stub = models.BooleanField(default=False, db_index=True)

    created_at = models.DateTimeField(default=timezone.now)
    last_login = models.DateTimeField(null=True, blank=True)
    questions_count = models.IntegerField(default=0)
    trust_score = models.FloatField(default=0.0)
    must_change_password = models.BooleanField(default=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    auto_renew_days = models.IntegerField(default=0)
    last_study_date = models.DateField(null=True, blank=True)
    current_streak = models.IntegerField(default=0)
    longest_streak = models.IntegerField(default=0)

    # ── Capability overrides ──────────────────────────────────────────
    #
    # A dict of {capability_string: bool}. Absent keys inherit the
    # role default. `True` grants even if the role does not hold it;
    # `False` revokes even if the role does. See
    # apps/users/services/permission_service.py for the resolution
    # order and caching behavior.
    capabilities = models.JSONField(default=dict, blank=True)

    # NOTE: the `theme_preference` field was removed. Theme is a
    # client-local preference (localStorage['theme']) written by the
    # useTheme composable on the frontend. It is never read or written
    # server-side. A column that no code consults is a liability: it
    # shows up in dumps, misleads readers into thinking it is
    # authoritative, and invites a future contributor to "fix" the
    # sync path.

    objects = UserManager()

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.username

    # ── Role properties ───────────────────────────────────────────────
    #
    # NOTE ON ROLE NAMES: the string 'admin' is checked literally in
    # `is_admin` here and in resolve_for_user() in
    # apps/users/services/permission_service.py. Those two checks
    # remain role-based on purpose:
    #
    #   • `is_admin` — a legitimate identity query. Templates and
    #     serializers use it for display ("Admin" badge).
    #   • `resolve_for_user` — the capability resolution short-circuit
    #     MUST be role-based, because a capability cannot grant
    #     capabilities without a chicken-and-egg problem.
    #
    # Every OTHER role-identity check has been converted to a
    # capability check. In particular, `is_expired` and
    # `renew_if_eligible` below now use 'system.bypass_expiry' rather
    # than `role == 'admin'`. That means a deployment can grant
    # expiry exemption to a moderator or a specific member without
    # granting them anything else.

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def is_moderator(self):
        return self.role == 'moderator'

    @property
    def is_member(self):
        return self.role == 'member'

    @property
    def is_expired(self):
        # The order of these two checks is load-bearing for cost, not
        # for correctness. `expires_at` is a plain field read; the
        # capability lookup walks the role default from cache and
        # applies per-user overrides. On a deployment where the vast
        # majority of users never have an `expires_at` value, the
        # null-field short-circuit below avoids the capability lookup
        # on nearly every call. Reordering these two branches does
        # not change the truth table — a user with null `expires_at`
        # and a user with `system.bypass_expiry` both return False
        # either way.
        if not self.expires_at:
            return False
        # Holders of 'system.bypass_expiry' are never subject to
        # expiry. Admin holds it automatically via the capability
        # resolution short-circuit; a deployment may grant it to any
        # other role or user through the panel or a per-user
        # override.
        if self.has_capability('system.bypass_expiry'):
            return False
        return self.expires_at <= timezone.now()

    # ── Capability API ────────────────────────────────────────────────

    def resolved_capabilities(self):
        """
        Return the full set of capabilities granted to this user.
        Memoized per request. See permission_service for details.
        """
        from apps.users.services.permission_service import (
            resolved_capabilities_for_user,
        )
        return resolved_capabilities_for_user(self)

    def has_capability(self, capability):
        if not capability:
            return False
        return capability in self.resolved_capabilities()

    def has_any_capability(self, *capabilities):
        if not capabilities:
            return False
        caps = self.resolved_capabilities()
        return any(c in caps for c in capabilities)

    def has_all_capabilities(self, *capabilities):
        if not capabilities:
            return True
        caps = self.resolved_capabilities()
        return all(c in caps for c in capabilities)

    # ── Author reputation ─────────────────────────────────────────────

    AUTHOR_RANK_THRESHOLDS = (
        (50, 90.0, 'authority'),
        (25, 75.0, 'expert'),
        (10, 50.0, 'contributor'),
        (3,  0.0,  'apprentice'),
    )

    @property
    def author_rank(self):
        if self.is_admin:
            return 'admin'
        total = self.questions_count or 0
        if total < 3:
            return 'newcomer'
        trust = self.trust_score or 0.0
        for min_q, min_t, key in self.AUTHOR_RANK_THRESHOLDS:
            if total >= min_q and trust >= min_t:
                return key
        return 'newcomer'

    @property
    def author_rank_label_ar(self):
        return {
            'admin':       'مدير النظام',
            'authority':   'مرجع معتمد',
            'expert':      'كاتب محترف',
            'contributor': 'كاتب متميز',
            'apprentice':  'كاتب موثوق',
            'newcomer':    'كاتب جديد',
        }.get(self.author_rank, 'كاتب جديد')

    # ── Lifecycle ─────────────────────────────────────────────────────

    def renew_if_eligible(self):
        # Same cost-ordering rationale as `is_expired` above: the two
        # cheap field checks run first, and the capability lookup only
        # executes on the narrow path where the account is actually
        # subject to renewal (has an `expires_at` and a positive
        # `auto_renew_days`). Truth table is unchanged.
        if not self.expires_at or self.auto_renew_days <= 0:
            return False
        # A holder of 'system.bypass_expiry' has nothing to renew —
        # they are already exempt from expiry.
        if self.has_capability('system.bypass_expiry'):
            return False
        now = timezone.now()
        if self.expires_at > now:
            return False
        self.expires_at = now + timedelta(days=self.auto_renew_days)
        return True

    def deactivate(self):
        """
        The everyday "remove this user" action. Sets `is_active=False`
        without deleting the row.

        This is the intended path for removing a user whose content
        is referenced elsewhere. With PROTECT on the authorship FKs,
        a hard delete is refused while any question references them;
        `deactivate()` is what the admin UI calls instead.
        """
        self.is_active = False
        self.save(update_fields=['is_active'])

    def update_trust_score(self):
        """
        Recompute this user's trust_score and questions_count.

        Delegates to AuthorReputationService.refresh_user so the two
        maintenance paths for these two fields — this per-user method
        and the batch `manage.py refresh_author_ranks` command — share
        one implementation.

        Ownership is resolved via the `authored_by` FK (see the
        docstring on Question.authored_by).

        Returns True if a write occurred, False if the values already
        matched. Callers that ignore the return value are unaffected.
        """
        from apps.questions.services.author_reputation_service import (
            AuthorReputationService,
        )
        return AuthorReputationService.refresh_user(self)

    def get_id(self):
        return str(self.id)

    def record_study_day(self):
        today = timezone.localdate()
        if self.last_study_date == today:
            return
        if self.last_study_date is not None and self.last_study_date == today - timedelta(days=1):
            self.current_streak = (self.current_streak or 0) + 1
        else:
            self.current_streak = 1
        self.last_study_date = today
        if self.current_streak > (self.longest_streak or 0):
            self.longest_streak = self.current_streak
        self.save(update_fields=['last_study_date', 'current_streak', 'longest_streak'])


class RoleCapabilities(models.Model):
    """
    Role → capability-set mapping used by the panel.

    One row per role. The `capabilities` list is a subset of
    apps.users.capabilities.CAPABILITIES. Anything outside that
    constant is filtered out at resolution time, so a stale row cannot
    grant a capability that no longer exists in the code.

    The 'admin' row is present but inert — resolve_for_user()
    short-circuits the admin role to the full CAPABILITIES set and
    never consults this row. Do not delete it; keeping it visible
    makes the panel's read-back consistent.
    """
    role = models.CharField(max_length=20, unique=True)
    capabilities = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Role capabilities'
        verbose_name_plural = 'Role capabilities'
        ordering = ['role']

    def __str__(self):
        return f'{self.role} ({len(self.capabilities or [])} caps)'


class ActiveSession(models.Model):
    session_id = models.CharField(max_length=36, primary_key=True)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='active_sessions',
    )
    ip = models.CharField(max_length=45, null=True, blank=True)
    user_agent = models.CharField(max_length=255, null=True, blank=True)
    last_seen = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Active: {self.user.username} ({self.session_id})"


class LoginAttempt(models.Model):
    ip = models.CharField(max_length=45)
    username = models.CharField(max_length=80, blank=True, null=True)
    attempt_time = models.DateTimeField(default=timezone.now)
    success = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.username or self.ip} - {self.attempt_time}"