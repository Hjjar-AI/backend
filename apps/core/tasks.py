# backend/apps/core/tasks.py

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.exams.models import ExamSession
from apps.users.models import User, ActiveSession
from apps.users.services import LoginSecurityService

logger = logging.getLogger(__name__)


def cleanup_database():
    """
    Aggregate housekeeping. Idempotent.

    Each sub-task is independent; a failure in one does not abort
    the others. Failures are surfaced in the return string so a
    scheduled run leaves a trail in its logs.
    """
    results = []

    def run_step(label, operation):
        try:
            results.append(operation())
        except Exception:
            logger.exception('Database cleanup failed: %s', label)
            results.append(f'{label} failed')

    # ── Exam sessions ─────────────────────────────────────────────
    def prune_exam_sessions():
        from apps.exams.services import ExamService

        cutoff = timezone.now() - timedelta(days=1)
        ExamService.discard_sessions(
            ExamSession.objects.filter(is_active=False, started_at__lt=cutoff),
        )
        stale_cutoff = timezone.now() - timedelta(days=7)
        ExamService.discard_sessions(
            ExamSession.objects.filter(is_active=True, started_at__lt=stale_cutoff),
        )
        return 'exam sessions pruned'

    run_step('exam sessions', prune_exam_sessions)

    # ── Master exam attempts ──────────────────────────────────────
    # The sweeper itself is grace-aware and idempotent, so calling it
    # here is safe even if the lazy path already finished some of
    # them.
    def finish_master_exams():
        from apps.master_exams.services.master_exam_sweeper import MasterExamSweeper
        finished = MasterExamSweeper.sweep_expired(limit=500)
        return f'{finished} master exam attempt(s) force-finished'

    run_step('master exam attempts', finish_master_exams)

    # ── Login attempts ────────────────────────────────────────────
    def prune_login_attempts():
        LoginSecurityService().cleanup_old_attempts()
        return 'login attempts pruned'

    run_step('login attempts', prune_login_attempts)

    # ── Active sessions ───────────────────────────────────────────
    def prune_active_sessions():
        active_cutoff = timezone.now() - timedelta(hours=1)
        ActiveSession.objects.filter(last_seen__lt=active_cutoff).delete()
        return 'active sessions pruned'

    run_step('active sessions', prune_active_sessions)

    # ── Privileged action audit log ───────────────────────────────
    run_step('audit rows', lambda: f'{cleanup_privileged_actions()} audit rows pruned')

    return 'Database cleanup completed: ' + ', '.join(results)


def privileged_action_retention(retention_days=None):
    if retention_days is None:
        retention_days = getattr(settings, 'PRIVILEGED_ACTION_RETENTION_DAYS', 365)
    days = max(1, int(retention_days))
    return days, timezone.now() - timedelta(days=days)


def cleanup_privileged_actions(retention_days=None):
    """
    Delete PrivilegedAction rows older than the retention window.

    The window is defined by PRIVILEGED_ACTION_RETENTION_DAYS
    (default 365). Pass `retention_days` explicitly to override —
    the management command uses this to support a `--days N` flag,
    and tests use it to bound the sweep deterministically.

    Returns the number of rows deleted.
    """
    from apps.core.models import PrivilegedAction

    _, cutoff = privileged_action_retention(retention_days)
    deleted, _ = PrivilegedAction.objects.filter(timestamp__lt=cutoff).delete()
    return deleted


def renew_expired_users():
    users = User.objects.filter(
        is_active=True,
        expires_at__lte=timezone.now(),
        auto_renew_days__gt=0,
    ).exclude(role='admin')
    renewed = 0
    for user in users:
        if user.renew_if_eligible():
            user.save()
            renewed += 1
    return f"{renewed} users renewed"


def cleanup_temp_files():
    from apps.core.utils import cleanup_old_temp_files
    deleted = cleanup_old_temp_files()
    return f"{deleted} files deleted"
