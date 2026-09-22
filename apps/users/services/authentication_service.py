# backend/apps/users/services/authentication_service.py

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone

from ..models import User, LoginAttempt
from .login_security_service import LoginSecurityService


class AuthenticationService:

    # Session lifetime per role, in seconds.
    #
    # This constant is the ONLY place the policy lives. It is not
    # duplicated in settings.py — a duplicated policy is how the
    # value drifts. If you ever want this configurable from an
    # environment variable, wire it to a single settings key and
    # read it here, but do not fork the definition.
    #
    # Roles present in this deployment (see User.ROLE_CHOICES):
    #   • 'admin'     — 8 hours. Admins hold every capability and
    #                   handle database/backup operations; a shorter
    #                   session limits blast radius if a token leaks.
    #   • 'moderator' — 7 days.
    #   • 'member'    — falls through to settings.SESSION_COOKIE_AGE
    #                   (14 days) via request.session.set_expiry(None).
    SESSION_LIFETIME_BY_ROLE = {
        'admin':     8 * 60 * 60,        # 8 hours
        'moderator': 7 * 24 * 60 * 60,   # 7 days
        # 'member' → None → 14 days (SESSION_COOKIE_AGE)
    }

    @staticmethod
    def login_user(username, password, request=None):
        login_security = LoginSecurityService()
        ip = request.META.get('REMOTE_ADDR', '') if request else ''

        if login_security.is_blocked(ip, username):
            return None

        user = authenticate(request=request, username=username, password=password)
        if user is None:
            login_security.record_attempt(ip, username, False)
            return None

        # NOTE: stub users (is_stub=True) never reach this point.
        # Django's default ModelBackend calls user_can_authenticate(),
        # which returns False for is_active=False — and every stub is
        # created with is_active=False AND an unusable password
        # (`make_password(None)`). So `authenticate()` above returns
        # None for a stub, and the `if user is None` branch records
        # the failed attempt with the correct audit trail.
        #
        # If a future change replaces ModelBackend with a custom
        # backend that skips `user_can_authenticate`, add an explicit
        # `if user.is_stub: return None` guard right here. Until then
        # it is redundant, and adding it would create an unreachable
        # code path that future readers would have to reason about.

        if not user.is_active:
            login_security.record_attempt(ip, username, False)
            return None

        # Expiry gate.
        #
        # No `role == 'admin'` bypass here. The exemption for accounts
        # that should never expire is enforced inside the two methods
        # called below, both of which short-circuit on the
        # 'system.bypass_expiry' capability:
        #
        #   • User.renew_if_eligible — returns False for bypass holders
        #   • User.is_expired        — returns False for bypass holders
        #
        # Admin holds the capability automatically via the
        # resolve_for_user() short-circuit. A deployment may grant it
        # to any other role or user through the permissions panel, and
        # the login path honors that grant without a code change.
        #
        # The outer `if user.expires_at` guard is a cheap opt-out for
        # accounts with no expiry at all — it skips the capability
        # lookup on a path that most users hit on every login.
        if user.expires_at:
            if user.renew_if_eligible():
                user.save(update_fields=['expires_at'])
            if user.is_expired:
                login_security.record_attempt(ip, username, False)
                return None

        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])
        login(request, user)

        if request is not None and hasattr(request, 'session'):
            lifetime = AuthenticationService.SESSION_LIFETIME_BY_ROLE.get(user.role)
            request.session.set_expiry(lifetime)

        login_security.record_attempt(ip, username, True)
        login_security.clear_failed_attempts_for_username(username)

        return user

    @staticmethod
    def logout_user(request):
        logout(request)

    @staticmethod
    def change_password(request, user, current_password, new_password):
        if not user.check_password(current_password):
            return False, 'كلمة المرور الحالية غير صحيحة'

        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as e:
            return False, ' '.join(e.messages)

        user.set_password(new_password)
        user.must_change_password = False
        user.save()
        update_session_auth_hash(request, user)

        return True, 'تم تغيير كلمة المرور بنجاح'