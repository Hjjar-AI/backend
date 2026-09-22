# backend/apps/users/services/login_security_service.py
from django.utils import timezone
from datetime import timedelta
from ..models import LoginAttempt


class LoginSecurityService:

    def __init__(self, max_attempts=5, block_minutes=15, ip_max_attempts=30):
        self.max_attempts = max_attempts
        self.block_minutes = block_minutes
        self.ip_max_attempts = ip_max_attempts

    def is_blocked(self, ip, username):
        cutoff = timezone.now() - timedelta(minutes=self.block_minutes)
        base_qs = LoginAttempt.objects.filter(
            attempt_time__gt=cutoff,
            success=False,
        )
        if base_qs.filter(ip=ip).count() >= self.ip_max_attempts:
            return True
        if username and base_qs.filter(ip=ip, username=username).count() >= self.max_attempts:
            return True
        return False

    def record_attempt(self, ip, username, success):
        LoginAttempt.objects.create(
            ip=ip,
            username=username,
            success=success
        )

    def clear_failed_attempts_for_username(self, username):
        """
        Delete the failure history for a username after a successful
        login, so a legitimate user is not one typo away from a new
        block immediately after proving possession of the password.

        Scoped to `success=False` rows so the successful attempt that
        just landed (written by record_attempt(..., True) before this
        method runs) remains in the table as an audit trail.

        Deliberately NOT clearing by IP: the IP bucket may contain
        failures from an entirely different user behind the same NAT.
        """
        if not username:
            return 0
        deleted, _ = LoginAttempt.objects.filter(
            username=username,
            success=False,
        ).delete()
        return deleted

    def cleanup_old_attempts(self):
        cutoff = timezone.now() - timedelta(minutes=self.block_minutes + 60)
        LoginAttempt.objects.filter(attempt_time__lt=cutoff).delete()