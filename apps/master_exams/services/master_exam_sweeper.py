# backend/apps/master_exams/services/master_exam_sweeper.py
"""
Sweeper for expired master exam attempts.

Two entry points, both grace-aware:

  • `sweep_expired(limit)` — global scan. Used by the cron-friendly
    `manage.py sweep_master_exams` command and by the cache-gated
    call inside the results dashboard. Iterates every expired,
    non-complete attempt (bounded by `limit`) and force-finishes
    each one. Returns the number of attempts it actually wrote.

  • `sweep_attempt(attempt)` — single-attempt check. Used by the
    status polling view. Returns True ONLY IF THIS CALL WROTE — i.e.
    it force-finished the attempt because the deadline plus grace
    had passed. Returns False if the attempt is still within its
    window, or was already complete before the call.

WHY A SINGLE-ATTEMPT VARIANT EXISTS
-----------------------------------
Before this change, `MasterExamAttemptStatusView.get` called
`sweep_expired(limit=50)` on every status poll. That endpoint is hit
every 30 seconds by every student taking an active exam. For a class
of 30 that is ~3,600 database-wide scans per hour, almost all of
which find nothing until the deadline passes for anyone.

The single-attempt variant looks at exactly the one row the caller
is already fetching. It preserves the same grace-window semantics —
an attempt is only force-finished once `deadline_at + grace` has
passed — but it costs one indexed read of a row that was already in
the query planner, not a fresh table scan.

GRACE-WINDOW SOURCE
-------------------
`_grace_seconds()` is imported from the attempt-service helpers
module rather than defined locally. The two definitions used to be
byte-identical copies of the same `getattr(settings, …)` read; a
future change to the grace window (e.g. reading from a runtime
Setting row instead of a settings constant) must land in one place
or the sweep and the submit path will disagree about whether an
attempt is still inside its window.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from ..models import MasterExamAttempt
from .master_exam_attempt_service.helpers import _grace_seconds

logger = logging.getLogger(__name__)


class MasterExamSweeper:

    @staticmethod
    def sweep_expired(limit=200):
        """
        Global sweep. Force-finish every incomplete attempt whose
        deadline plus grace has passed. Returns the number of
        attempts this run actually finished.
        """
        from .master_exam_attempt_service import MasterExamAttemptService

        now = timezone.now()
        grace = timedelta(seconds=_grace_seconds())

        expired = list(
            MasterExamAttempt.objects
            .filter(is_complete=False, deadline_at__lte=now - grace)
            .order_by('deadline_at')[:limit]
        )

        finished = 0
        for attempt in expired:
            try:
                MasterExamAttemptService.finish(attempt, forced=True)
                finished += 1
            except Exception:
                logger.exception(
                    'Sweeper failed to finish attempt %s', attempt.id,
                )

        if finished:
            logger.info('Master exam sweeper finished %d expired attempt(s)', finished)

        return finished

    @staticmethod
    def sweep_attempt(attempt):
        """
        Single-attempt sweep.

        RETURN CONTRACT
        ---------------
        True  — this call force-finished the attempt (the row was
                written; the caller should refresh_from_db()).
        False — no write happened. Either the attempt is still
                within its window, or it was already complete before
                the call.

        The "already complete" case is False on purpose: the caller's
        refresh_from_db() is only needed when THIS call changed the
        row, and firing it after every status check on a completed
        attempt would add one query per poll for the rest of the
        session.

        `attempt=None` returns False — used by callers that have not
        yet verified the attempt exists and want a single-line guard.
        """
        if attempt is None:
            return False
        if attempt.is_complete:
            return False

        now = timezone.now()
        grace = timedelta(seconds=_grace_seconds())
        if attempt.deadline_at > now - grace:
            return False

        from .master_exam_attempt_service import MasterExamAttemptService

        try:
            MasterExamAttemptService.finish(attempt, forced=True)
            return True
        except Exception:
            logger.exception(
                'Sweeper failed to finish attempt %s', attempt.id,
            )
            return False