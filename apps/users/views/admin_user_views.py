# backend/apps/users/views/admin_user_views.py

import secrets
from datetime import timedelta

from rest_framework.views import APIView

from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from ..serializers import (
    UserSerializer,
    UserCreateSerializer,
    UserUpdateSerializer,
    AdminResetPasswordSerializer,
)
from ..models import User, ActiveSession
from apps.exams.models import TestHistory
from apps.core.permissions import HasCapability
from apps.core.utils import api_success, api_error, paginate
from apps.core.audit import log_privileged_action
from apps.core.throttles import AdminPasswordRateThrottle
from apps.core.reauth import admin_password_matches


# ═════════════════════════════════════════════════════════════════════
# Shared helpers
# ═════════════════════════════════════════════════════════════════════


def _require_admin_password(request):
    """
    Re-authenticate the acting admin for a privileged write.

    Returns None on success, or an `api_error` response on failure.

    CONSOLIDATION (fix — same rule, five different implementations)
    ---------------------------------------------------------------
    The "read admin_password from request.data, check it against the
    acting user, return 403 on mismatch" pattern was hand-inlined in
    four view methods here (AdminUserListView.post,
    AdminUserDetailView.put, AdminUserDetailView.delete,
    AdminToggleUserView.post) and implemented a *fifth* different way
    inside AdminResetPasswordView.post (via a serializer field
    instead of a raw dict read). Same rule, two styles, five places.

    The helper is now the one place. AdminResetPasswordView keeps its
    serializer because the serializer also validates the optional
    new_password field in the same pass — but the admin-password
    check inside it is unchanged in effect.
    """
    raw = request.data.get('admin_password')
    if not admin_password_matches(request.user, raw):
        return api_error('كلمة مرور المدير غير صحيحة', 403)
    return None


def _set_password_and_log(request, user, password, *, mode):
    """
    Apply a password reset to `user` and write the audit row.

    Both branches of AdminResetPasswordView.post (admin-supplied
    password vs. generated temporary password) executed the same
    five-step sequence — `set_password`, `must_change_password=True`,
    `save`, conditional `update_session_auth_hash`, `log_privileged_
    action` — with only the `mode` field of the audit `details`
    differing. Extracting the sequence removes ~20 duplicated lines
    and guarantees the two branches cannot drift on any of the
    subsequent steps (a plausible future edit being "the self-reset
    case should keep the session, but the handed-out case should
    not" — that change belongs in one place, not two).
    """
    user.set_password(password)
    user.must_change_password = True
    user.save()

    if user.id == request.user.id:
        update_session_auth_hash(request, user)

    log_privileged_action(
        request,
        action='user.reset_password',
        target=user,
        target_repr=user.username,
        details={
            'mode': mode,
            'self_reset': user.id == request.user.id,
            'was_stub': user.is_stub,
        },
    )


def _expiry_days_left(user, now):
    """
    Days until expiry, or None when the value is not meaningful.

    A holder of 'system.bypass_expiry' never expires regardless of
    what `expires_at` contains (see User.is_expired). Reporting a
    "days left" value for such a user is misleading:

      • If `expires_at` is in the past, the value is negative, and
        the admin UI renders "Expired" (red) for an account that
        can still log in — actively wrong.
      • If `expires_at` is in the future, the value understates the
        account's real state — the account has no expiry.

    None is the honest answer for both cases, and the admin UI's
    `statUnlimited` label already covers None.
    """
    if not user.expires_at:
        return None
    if user.has_capability('system.bypass_expiry'):
        return None
    return (user.expires_at - now).days


def _add_latest_exam_fields(data, user, latest, now):
    accuracy = latest.get('accuracy') if isinstance(latest, dict) else (
        latest.accuracy if latest else None
    )
    tag = latest.get('tag') if isinstance(latest, dict) else (
        latest.tag if latest else None
    )
    data['latest_exam_accuracy'] = round(accuracy, 1) if accuracy is not None else None
    data['latest_exam_tag'] = tag if accuracy is not None else None
    data['expiry_days_left'] = _expiry_days_left(user, now)
    return data


def _acquire_admin_lock_set(target_user_id):

    # ── Pre-read (unlocked) to size the lock set ───────────────────
    probe = (
        User.objects
        .filter(id=target_user_id)
        .only('id', 'role', 'is_active')
        .first()
    )
    if probe is None:
        return None, []

    if probe.role == 'admin' and probe.is_active:
        other_ids = list(
            User.objects
            .filter(role='admin', is_active=True)
            .exclude(id=probe.id)
            .values_list('id', flat=True)
        )
    else:
        other_ids = []

    lock_ids = sorted(set([probe.id] + other_ids))

    # ── Acquisition in canonical order ─────────────────────────────
    #
    # One SELECT ... FOR UPDATE per row, in ascending id order. A
    # single `WHERE id IN (...) FOR UPDATE` relies on the storage
    # engine's index-scan order for lock acquisition; that happens
    # to be ascending for a PK IN list in InnoDB, but it is not a
    # documented guarantee. The explicit loop removes the dependency
    # on that behavior at the cost of N small queries (N = number
    # of admins in the deployment, typically 1–3).
    locked = {}
    for uid in lock_ids:
        row = (
            User.objects
            .select_for_update()
            .filter(id=uid)
            .first()
        )
        if row is not None:
            locked[uid] = row

    target = locked.get(probe.id)

    # ── Post-lock state re-check ──────────────────────────────────
    other_active_admin_ids = [
        uid for uid, row in locked.items()
        if uid != probe.id and row.role == 'admin' and row.is_active
    ]
    return target, other_active_admin_ids


class AdminUserListView(APIView):
    """
    GET  — paginated user list with aggregated latest-exam columns.
    POST — create a new user. Requires admin password re-auth.

    Stub users are excluded from the list — they are import-created
    author records, not real accounts, and are managed from the
    ExternalAuthorMapping admin page or by promoting them to a real
    user through the User admin. The count in the pagination meta
    reflects the same filter, so "total" means "total real users".

    LATEST-EXAM COLUMNS — BATCHED FETCH
    -----------------------------------
    The `latest_exam_accuracy` and `latest_exam_tag` columns used to
    come from two per-row correlated Subqueries. On a page of N
    users that was 2·N subquery executions, each an index scan on
    `(user_id, -completed_at)`. MariaDB in particular does not
    decorrelate these, so the cost grew linearly with page size and
    row count.

    The implementation now issues two additional queries for the
    whole page, independent of N:

      1. `MAX(completed_at)` per user in the current page.
      2. One `OR` query that fetches the specific
         `(user_id, completed_at)` rows selected in step 1, carrying
         only the two fields the response needs (`accuracy`, `tag`).

    Both queries are bounded by the page size (max 500 by the
    paginate() clamp) and hit the same `(user_id, completed_at)`
    index. Total cost is now 4 queries per page (count + page +
    max-per-user + matching-rows) regardless of how many users are
    on the page.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.users'

    def get_throttles(self):
        if self.request.method == 'POST':
            return [AdminPasswordRateThrottle()]
        return []

    def get(self, request):
        users_qs = (
            User.objects
            .filter(is_stub=False)
            .order_by('-created_at')
        )
        page, meta = paginate(users_qs, request)

        # Materialize the page once — `paginate` returns a sliced
        # queryset, and iterating it twice would re-run the query.
        page_list = list(page)
        page_ids = [u.id for u in page_list]

        # ── Batch: latest TestHistory row per user on this page ────
        latest_by_user = {}
        if page_ids:
            # Step 1: the latest completed_at timestamp per user.
            latest_times_qs = (
                TestHistory.objects
                .filter(user_id__in=page_ids)
                .values('user_id')
                .annotate(latest_completed_at=Max('completed_at'))
            )
            latest_time_by_user = {
                row['user_id']: row['latest_completed_at']
                for row in latest_times_qs
            }

            # Step 2: the specific row matching each (user, timestamp)
            # pair. One OR clause per user on the page — bounded by
            # the page size and hit against the composite index.
            if latest_time_by_user:
                q = Q()
                for uid, ts in latest_time_by_user.items():
                    q |= Q(user_id=uid, completed_at=ts)
                latest_rows = (
                    TestHistory.objects
                    .filter(q)
                    .order_by('user_id', '-id')
                    .values('user_id', 'accuracy', 'tag')
                )
                for row in latest_rows:
                    latest_by_user.setdefault(row['user_id'], row)

        now = timezone.now()
        data = []
        for user in page_list:
            user_data = UserSerializer(user).data

            data.append(_add_latest_exam_fields(
                user_data, user, latest_by_user.get(user.id), now,
            ))

        return api_success(data={'items': data, **meta})

    def post(self, request):
        error = _require_admin_password(request)
        if error is not None:
            return error

        serializer = UserCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.save()

        log_privileged_action(
            request,
            action='user.create',
            target=user,
            target_repr=user.username,
            details={
                'role': user.role,
                'is_active': user.is_active,
                'capability_overrides': user.capabilities or {},
            },
        )

        data = UserSerializer(user).data
        data['latest_exam_accuracy'] = None
        data['latest_exam_tag'] = None
        data['expiry_days_left'] = _expiry_days_left(user, timezone.now())

        return api_success(data=data, message='تم إنشاء المستخدم بنجاح', code=201)


class AdminUserDetailView(APIView):
    """
    GET    — one user with the same aggregated columns as the list.
    PUT    — update. Requires admin password re-auth.
    DELETE — delete. Requires admin password re-auth.

    A stub user is intentionally reachable through this endpoint (the
    admin may need to inspect or promote it), but promoting it to a
    real account must be done from Django admin — this endpoint
    cannot flip `is_stub` (the field is read-only on the update
    serializer). The intent is that promotion is an explicit admin
    action, not a side effect of an edit.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.users'

    def get_throttles(self):
        if self.request.method in ('PUT', 'DELETE'):
            return [AdminPasswordRateThrottle()]
        return []

    def get(self, request, user_id):
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return api_error('المستخدم غير موجود', 404)

        serializer = UserSerializer(user)
        data = serializer.data

        latest_session = user.test_history.order_by('-completed_at', '-id').first()
        _add_latest_exam_fields(data, user, latest_session, timezone.now())

        return api_success(data=data)

    def put(self, request, user_id):
        if user_id == request.user.id:
            return api_error('لا يمكنك تعديل نفسك', 400)

        error = _require_admin_password(request)
        if error is not None:
            return error

        # ── Validate against a probe instance, BEFORE locking ─────
        #
        # `UserUpdateSerializer.validate_new_password` runs
        # Django's password validators with `user=self.instance`, so
        # the similarity check needs a real user (username, email,
        # full_name populated) — not a stub with only `id`. Fetching
        # the real row here is also what feeds the demote/deactivate
        # signal computation below, so we do it once, unlocked, and
        # then re-read under the lock.
        try:
            user_probe = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return api_error('المستخدم غير موجود', 404)

        serializer = UserUpdateSerializer(
            user_probe, data=request.data, partial=True,
        )
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        # Boolean signals for the "last active admin" guard below.
        # The actual check runs under the row lock, so these are
        # only used to decide whether a lock-and-recheck is
        # warranted. Both are cheap.
        requested_demote = (
            'role' in validated
            and str(validated['role']).strip() != 'admin'
        )
        requested_deactivate = (
            'is_active' in validated
            and validated['is_active'] is False
        )

        with transaction.atomic():
            # Canonical lock order — same pattern as delete() and
            # AdminToggleUserView.post(). This is what closes the
            # race in which two admins concurrently PUT each other
            # to role='member' and the system ends up with zero
            # active admins.
            user, other_active_admin_ids = _acquire_admin_lock_set(user_id)
            if user is None:
                return api_error('المستخدم غير موجود', 404)

            # The guard only fires when the target IS the last
            # active admin AND the request would strip that status.
            # A PUT that changes nothing about role or is_active on
            # the last admin is unaffected.
            if (
                user.is_admin
                and user.is_active
                and not other_active_admin_ids
                and (requested_demote or requested_deactivate)
            ):
                return api_error(
                    'لا يمكن إزالة أو تعطيل آخر مدير في النظام',
                    400,
                )

            # Rebind the serializer's instance to the row we hold
            # the lock on. The probe instance may have been replaced
            # by a concurrent writer between the probe read and the
            # lock acquisition; the locked row is authoritative.
            # `validated_data` is already computed and is reused.
            serializer.instance = user

            password_changed = (
                'new_password' in validated
                and bool(validated.get('new_password'))
            )

            changes = {}
            for field in ('full_name', 'role', 'is_active', 'expiry_days', 'auto_renew_days'):
                if field in validated:
                    changes[field] = validated[field]
            if password_changed:
                changes['password_reset'] = True
            if 'capabilities' in validated:
                changes['capability_override_count'] = len(
                    validated.get('capabilities') or {}
                )

            user = serializer.save()

        log_privileged_action(
            request,
            action='user.update',
            target=user,
            target_repr=user.username,
            details=changes,
        )

        data = UserSerializer(user).data

        latest_session = user.test_history.order_by('-completed_at', '-id').first()
        _add_latest_exam_fields(data, user, latest_session, timezone.now())
        data['password_changed'] = password_changed

        return api_success(data=data, message='تم تحديث المستخدم')

    def delete(self, request, user_id):
        if user_id == request.user.id:
            return api_error('لا يمكنك حذف نفسك', 400)

        error = _require_admin_password(request)
        if error is not None:
            return error

        # Locks are acquired inside the atomic block, in canonical
        # order. See `_acquire_admin_lock_set` for the ordering
        # rationale.
        with transaction.atomic():
            user, other_active_admin_ids = _acquire_admin_lock_set(user_id)
            if user is None:
                return api_error('المستخدم غير موجود', 404)

            if user.is_admin and user.is_active and not other_active_admin_ids:
                return api_error('لا يمكن حذف آخر مدير في النظام', 400)

            # PROTECT on Question.authored_by / owned_by blocks a raw
            # delete when the user has any question. Surface a clear
            # message rather than letting the ProtectedError turn
            # into a generic 500.
            from django.db.models import ProtectedError
            from apps.questions.models import Question

            if Question.objects.filter(
                authored_by=user,
            ).exists() or Question.objects.filter(
                owned_by=user,
            ).exists():
                return api_error(
                    'لا يمكن حذف هذا المستخدم لأنه مرتبط بأسئلة. '
                    'أعد تعيين ملكية أسئلته أو قم بتعطيل الحساب بدلاً من حذفه.',
                    409,
                )

            deleted_username = user.username
            deleted_id = user.id
            deleted_overrides = dict(user.capabilities or {})

            try:
                user.delete()
            except ProtectedError:
                return api_error(
                    'لا يمكن حذف هذا المستخدم لأنه مرتبط بسجلات أخرى.',
                    409,
                )

        log_privileged_action(
            request,
            action='user.delete',
            target=None,
            target_repr=f'user:{deleted_username}',
            details={
                'deleted_user_id': deleted_id,
                'deleted_username': deleted_username,
                'had_capability_overrides': bool(deleted_overrides),
            },
        )

        return api_success(message='تم حذف المستخدم')


class AdminToggleUserView(APIView):
    """
    Activate / deactivate one user. Requires admin password re-auth.

    Refuses to toggle a stub — a stub cannot log in regardless of its
    `is_active` value, and flipping the flag is a misleading no-op.
    Promote the stub to a real user first (clear `is_stub`, set a
    password, set `is_active=True`) from the Django admin.

    Uses the same canonical lock ordering as the delete view.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.users'
    throttle_classes = [AdminPasswordRateThrottle]

    def post(self, request, user_id):
        if user_id == request.user.id:
            return api_error('لا يمكنك تعطيل نفسك', 400)

        error = _require_admin_password(request)
        if error is not None:
            return error

        with transaction.atomic():
            user, other_active_admin_ids = _acquire_admin_lock_set(user_id)
            if user is None:
                return api_error('المستخدم غير موجود', 404)

            if user.is_stub:
                return api_error(
                    'هذا مستخدم وهمي (مؤلف خارجي). قم بترقيته إلى مستخدم '
                    'حقيقي من لوحة الإدارة أولاً.',
                    400,
                )

            if user.is_admin and user.is_active and not other_active_admin_ids:
                return api_error('لا يمكن تعطيل آخر مدير في النظام', 400)

            previous_state = user.is_active
            user.is_active = not user.is_active
            user.save()

            post_state = user.is_active

        log_privileged_action(
            request,
            action='user.toggle',
            target=user,
            target_repr=user.username,
            details={
                'is_active_before': previous_state,
                'is_active_after': post_state,
            },
        )

        status_msg = 'تم تفعيل المستخدم' if post_state else 'تم تعطيل المستخدم'
        return api_success(message=status_msg)


class AdminResetPasswordView(APIView):
    """
    Two modes, decided by whether new_password is present:

      • new_password provided  → set it, force change on next login.
      • new_password absent    → generate a one-time password, return
                                  it in the response.

    Reset is allowed on stubs — it is the natural first step of
    promoting a stub to a real user. The caller is still expected to
    clear `is_stub` and set `is_active=True` separately.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.users'
    throttle_classes = [AdminPasswordRateThrottle]

    def post(self, request, user_id):
        body = AdminResetPasswordSerializer(data=request.data)
        if not body.is_valid():
            return api_error('كلمة مرور المدير الحالية مطلوبة', 400, details=body.errors)

        if not admin_password_matches(request.user, body.validated_data['admin_password']):
            return api_error('كلمة مرور المدير غير صحيحة', 403)

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return api_error('المستخدم غير موجود', 404)

        new_password = body.validated_data['new_password']

        if new_password:
            try:
                validate_password(new_password, user=user)
            except DjangoValidationError as e:
                return api_error(' '.join(e.messages), 400)

            _set_password_and_log(
                request, user, new_password, mode='admin_supplied',
            )
            return api_success(message='تم إعادة تعيين كلمة المرور بنجاح')

        temp_password = secrets.token_urlsafe(12)
        _set_password_and_log(
            request, user, temp_password, mode='generated_temp',
        )

        return api_success(
            data={'temp_password': temp_password},
            message='تم إعادة تعيين كلمة المرور بنجاح',
        )


class AdminActiveUsersView(APIView):
    """
    Sessions seen within the last 5 minutes, keyed by user id.

    Stubs have no sessions (they cannot log in) so no explicit filter
    is needed — the ActiveSession query is already constrained to
    users that authenticated. The capability gate is
    'admin.active_users', not 'admin.users' — a deployment could
    grant live-activity visibility to a role that should not be able
    to create, edit, or delete accounts.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.active_users'

    def get(self, request):
        cutoff = timezone.now() - timedelta(minutes=5)

        sessions = (
            ActiveSession.objects
            .filter(last_seen__gt=cutoff)
            .select_related('user')
            .order_by('last_seen')
        )

        result = {}

        for s in sessions:
            user = s.user
            result[user.id] = {
                'name': user.full_name or user.username,
                'ip': s.ip,
                'role': user.role,
                'last_seen': s.last_seen.isoformat(),
            }

        return api_success(data={'users': result})