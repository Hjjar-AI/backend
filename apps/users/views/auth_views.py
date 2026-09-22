# backend/apps/users/views/auth_views.py
"""
Public authentication surface.

Login / logout / me / self-service password change / CSRF token. These
endpoints are reachable before the caller has a session (login, CSRF)
or operate on the caller's own session (logout, me, change-password).
None of them touch another user's account.

Admin password change is also hosted here (see
AdminPasswordChangeView) because it shares the auth-flow concern: it
is the piece of the flow that clears `must_change_password` on the
admin surface.
"""
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny

from django.contrib import admin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.views import View
from django.views.decorators.csrf import ensure_csrf_cookie
from django.utils.decorators import method_decorator
from django.middleware.csrf import get_token

from ..serializers import (
    UserSerializer,
    LoginSerializer,
    ChangePasswordSerializer,
)
from ..services import AuthenticationService
from apps.core.utils import api_success, api_error, set_csrf_cookie
from apps.core.throttles import (
    LoginCredentialsRateThrottle,
    LoginIpRateThrottle,
    CsrfRateThrottle,
    AdminPasswordRateThrottle,
)


class LoginView(APIView):
    permission_classes = [AllowAny]

    throttle_classes = [LoginCredentialsRateThrottle, LoginIpRateThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        username = serializer.validated_data['username']
        password = serializer.validated_data['password']

        user = AuthenticationService.login_user(username, password, request)

        if user is None:
            return api_error('اسم المستخدم أو كلمة المرور غير صحيحة', 401)

        data = {
            'user': UserSerializer(user).data,
        }

        response = api_success(data=data, message='تم تسجيل الدخول بنجاح', code=200)
        return set_csrf_cookie(response, request)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        AuthenticationService.logout_user(request)
        response = api_success(message='تم تسجيل الخروج')
        return set_csrf_cookie(response, request)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserSerializer(request.user)
        return api_success(data=serializer.data)


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    throttle_classes = [AdminPasswordRateThrottle]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        success, msg = AuthenticationService.change_password(
            request,
            request.user,
            serializer.validated_data['current_password'],
            serializer.validated_data['new_password'],
        )

        if not success:
            return api_error(msg, 400)

        return api_success(message=msg)


class GetCSRFTokenView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [CsrfRateThrottle]

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        return api_success(data={'token': get_token(request)})


class AdminPasswordChangeView(View):
    """
    Pass-through wrapper around Django admin's password-change view that
    clears ``must_change_password`` on the actor after a successful
    change.

    WHY THIS EXISTS
    ---------------
    ``MustChangePasswordMiddleware`` redirects ``/admin/*`` GET
    requests to ``/admin/password_change/`` when the acting user has
    ``must_change_password=True``. Django admin's own password-change
    view saves the new password hash and redirects to
    ``password_change_done``, but has no knowledge of our custom flag,
    so the flag would remain set and the next request to ``/admin/``
    would be redirected right back to the change-password form — an
    infinite loop.

    This wrapper delegates both GET and POST to
    ``admin.site.password_change`` (so the form rendering, validation,
    and error handling are Django's, unchanged) and intervenes only on
    POST-success to clear the flag. The success test is
    ``response.url == reverse('admin:password_change_done')`` — the
    same target Django's admin view uses on the valid-form path. Any
    other response (form with errors, login redirect for an
    unauthenticated request) is passed through untouched.

    AUTH BEHAVIOR
    -------------
    The raw ``admin.site.password_change`` method checks
    ``has_change_permission(request.user)`` (i.e. ``is_active`` and
    ``is_staff``) and raises ``PermissionDenied`` for an
    unauthenticated request. Django's default admin URL wraps this
    with ``admin_view``, which converts the ``PermissionDenied`` into
    a redirect to ``/admin/login/``. This wrapper reproduces that
    redirect so an anonymous request to this URL is handled the same
    way a request to any other admin URL would be — but only for the
    ``PermissionDenied`` path, not for any other exception.

    ADMIN URL PREFIX
    ----------------
    This override assumes the admin site is mounted at ``/admin/``,
    matching ``config/urls.py``. If a deployment ever changes the
    admin prefix (e.g. to ``/manage/``), the override path in
    ``config/urls.py`` must be updated to match, otherwise the
    middleware's admin redirect would loop against Django's default
    ``password_change`` URL. The check is a single grep for
    ``'admin/password_change/'`` in ``config/urls.py``.
    """

    def _dispatch_to_django_admin(self, request, *args, **kwargs):
        """
        Call Django's ``admin.site.password_change``, converting the
        ``PermissionDenied`` it raises for an unauthenticated or
        non-staff user into the same login redirect Django's own
        ``admin_view`` decorator would produce.
        """
        try:
            return admin.site.password_change(request, *args, **kwargs)
        except PermissionDenied:
            return redirect_to_login(
                request.get_full_path(),
                reverse('admin:login'),
            )

    def get(self, request, *args, **kwargs):
        return self._dispatch_to_django_admin(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        response = self._dispatch_to_django_admin(request, *args, **kwargs)

        # Success detection: Django's admin password-change view
        # redirects to the `password_change_done` URL after a valid
        # POST. Any other response (form with errors, login redirect)
        # is passed through without touching the flag.
        if (
            isinstance(response, HttpResponseRedirect)
            and response.url == reverse('admin:password_change_done')
        ):
            user = request.user
            if user.is_authenticated and user.must_change_password:
                user.must_change_password = False
                user.save(update_fields=['must_change_password'])

        return response