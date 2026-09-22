# tests/core/test_cleanup_database.py
"""
`tasks.cleanup_database` — the aggregate housekeeping function.

Each sub-task has its own coverage elsewhere. This file exercises
the aggregator itself: that it calls every sub-task and returns a
combined summary string. A regression that drops a sub-task from
the aggregator is invisible to the sub-task tests.
"""
from datetime import timedelta

from django.utils import timezone

from apps.core.models import PrivilegedAction
from apps.core.tasks import cleanup_database
from apps.exams.models import ExamSession
from apps.users.models import ActiveSession
from tests.base import CacheClearingTestCase
from tests.factories import make_exam_session, make_user


class CleanupDatabaseTests(CacheClearingTestCase):
    def test_returns_summary_string(self):
        result = cleanup_database()
        self.assertIn('Database cleanup completed', result)
        self.assertIn('exam sessions pruned', result)
        self.assertIn('login attempts pruned', result)
        self.assertIn('active sessions pruned', result)
        self.assertIn('audit rows pruned', result)
        self.assertIn('master exam attempt(s) force-finished', result)

    def test_prunes_inactive_exam_sessions(self):
        u = make_user('alice')
        stale = make_exam_session(u)
        stale.is_active = False
        stale.created_at = timezone.now() - timedelta(days=2)
        stale.save()

        cleanup_database()
        self.assertFalse(
            ExamSession.objects.filter(id=stale.id).exists(),
        )

    def test_prunes_stale_active_sessions(self):
        u = make_user('alice')
        stale = make_exam_session(u)
        stale.started_at = timezone.now() - timedelta(days=10)
        stale.save()

        cleanup_database()
        self.assertFalse(
            ExamSession.objects.filter(id=stale.id).exists(),
        )

    def test_prunes_old_audit_rows(self):
        PrivilegedAction.objects.create(
            action='test.old',
            timestamp=timezone.now() - timedelta(days=400),
        )
        cleanup_database()
        self.assertEqual(PrivilegedAction.objects.count(), 0)

    def test_prunes_inactive_active_session_rows(self):
        u = make_user('alice')
        ActiveSession.objects.create(
            session_id='stale-session',
            user=u,
            last_seen=timezone.now() - timedelta(hours=2),
        )
        cleanup_database()
        self.assertEqual(ActiveSession.objects.count(), 0)

    def test_keeps_recent_active_sessions(self):
        u = make_user('alice')
        ActiveSession.objects.create(
            session_id='fresh-session',
            user=u,
            last_seen=timezone.now(),
        )
        cleanup_database()
        self.assertEqual(ActiveSession.objects.count(), 1)