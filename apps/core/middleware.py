# backend/apps/core/middleware.py
import logging
import time

from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils import timezone

from apps.users.models import ActiveSession

logger = logging.getLogger(__name__)


class MustChangePasswordMiddleware:
    """
    Force a password change before a user with
    `must_change_password=True` can use the app.

    COVERS BOTH SURFACES — SPA AND DJANGO ADMIN
    -------------------------------------------
    The previous version short-circuited on any path not starting
    with `/api/`, which meant a staff user whose password had just
    been reset via `AdminResetPasswordView` (which sets
    `must_change_password=True`) could bypass the forced-change flow
    entirely by going straight to `/admin/`. Django admin is a
    distinct surface but it uses the same credential, and the flag's
    whole point is to invalidate a temporary credential before the
    holder can act on it.

    METHOD GATING
    -------------
    The admin branch redirects only on GET. A POST to an admin URL
    while the flag is set returns a plain 403 rather than a 302,
    because a 302 on a POST is silently converted to a GET by the
    browser and the request body is lost — the user would see the
    change-password form with no indication their submission was
    discarded. A 403 is the honest answer.

    ALLOWED PREFIXES
    ----------------
    Every endpoint that MUST stay reachable while the flag is set:
    the change-password flows on both surfaces, the logout endpoints,
    the SPA's bootstrap reads (`me`, `csrf`, `config`), and Django
    admin's login / logout / password-change / i18n endpoints.
    """

    ALLOWED_PREFIXES = (
        # ── SPA auth flow ─────────────────────────────────────────
        '/api/v1/auth/change-password/',
        '/api/v1/auth/logout/',
        '/api/v1/auth/me/',
        '/api/v1/auth/csrf/',
        '/api/v1/config/',
        # ── Django admin auth flow ────────────────────────────────
        '/admin/login/',
        '/admin/logout/',
        '/admin/password_change/',
        '/admin/jsi18n/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        is_api = path.startswith('/api/')
        is_admin = path.startswith('/admin/')

        if not (is_api or is_admin):
            return self.get_response(request)

        user = getattr(request, 'user', None)

        if (
            user is None
            or not user.is_authenticated
            or not getattr(user, 'must_change_password', False)
        ):
            return self.get_response(request)

        if any(path.startswith(p) for p in self.ALLOWED_PREFIXES):
            return self.get_response(request)

        if is_admin:
            if request.method == 'GET':
                return HttpResponseRedirect('/admin/password_change/')
            return HttpResponse(
                'يجب تغيير كلمة المرور قبل المتابعة.',
                status=403,
                content_type='text/plain; charset=utf-8',
            )

        return JsonResponse(
            {
                'code': 403,
                'message': 'يجب تغيير كلمة المرور قبل المتابعة.',
                'details': {'reason': 'MUST_CHANGE_PASSWORD'},
            },
            status=403,
        )


class ActiveSessionMiddleware:
    HEARTBEAT_SECONDS = 300

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.user.is_authenticated:
            self._track(request)
        return response

    def _track(self, request):
        now = time.time()
        last_tracked = request.session.get('_last_tracked', 0)
        if now - last_tracked < self.HEARTBEAT_SECONDS:
            return

        session_key = request.session.session_key
        if not session_key:
            request.session.save()
            session_key = request.session.session_key
            if not session_key:
                return

        ip = request.META.get('REMOTE_ADDR')
        user_agent = request.META.get('HTTP_USER_AGENT', '')[:255]
        now_dt = timezone.now()

        try:
            ActiveSession.objects.update_or_create(
                session_id=session_key,
                defaults={
                    'user': request.user,
                    'ip': ip,
                    'user_agent': user_agent,
                    'last_seen': now_dt,
                },
            )
        except Exception:
            # Swallow-and-continue is intentional — a failure to
            # update the presence table must not fail the request
            # that triggered it. But log it, so a persistent DB
            # failure is visible instead of silently degrading
            # presence tracking.
            logger.exception(
                'ActiveSession update failed (session_id=%s, user_id=%s)',
                session_key, getattr(request.user, 'id', None),
            )
            return

        request.session['_last_tracked'] = now
        request.session.modified = True


class AutoRenewMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.user.renew_if_eligible():
            request.user.save(update_fields=['expires_at'])
        response = self.get_response(request)
        return response