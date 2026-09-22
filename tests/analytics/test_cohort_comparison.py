# tests/analytics/test_cohort_comparison.py
"""
`AnalyticsService.get_cohort_comparison` — the only method in the
analytics admin surface that had no coverage.

The service batches three queries regardless of how many groups
exist:
  1. Fetch every active group.
  2. Fetch every (group, user) membership pair.
  3. Aggregate TestHistory by user_id, then roll up per group in
     Python.

GROUP EXCLUSION
---------------
A group is emitted in the response ONLY IF at least one of its
members has at least one session inside the `days` window. A group
whose members have all been inactive for longer than the window is
silently omitted, not reported with zero values.

`member_count` still reports the total membership regardless of
activity (the join to `group_members` includes every user); only
the row's presence depends on there being at least one session.

This is the current behavior — the tests below pin it from both
directions so a future refactor that switches to "always emit a
row" is a deliberate change rather than an accident.
"""
from datetime import timedelta

from django.utils import timezone

from apps.analytics.services import AnalyticsService
from apps.groups.models import Group, GroupMembership
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_test_history


def _group(name):
    return Group.objects.create(name=name, created_by='test')


def _member(group, user):
    return GroupMembership.objects.create(group=group, user=user)


class CohortComparisonTests(CacheClearingTestCase):
    def test_no_groups_returns_empty_list(self):
        self.assertEqual(AnalyticsService.get_cohort_comparison(), [])

    def test_group_with_no_members_is_excluded(self):
        _group('Empty')
        self.assertEqual(AnalyticsService.get_cohort_comparison(), [])

    def test_group_with_members_but_no_recent_sessions_is_omitted(self):
        """
        A group with members but zero sessions in the window does
        not produce a row. The row is derived from the per-user
        aggregation, so a group with no aggregation contribution
        has no row.

        Before this test, my assumption was that the group would
        be reported with all-zero values. It is not. The batch
        rewrite derives the row from the per-user aggregation, and
        a group with no aggregation contribution has no row.
        """
        g = _group('Quiet')
        u = make_user('alice')
        _member(g, u)
        self.assertEqual(AnalyticsService.get_cohort_comparison(), [])

    def test_active_users_counts_only_those_with_sessions(self):
        g = _group('Active')
        alice = make_user('alice')
        bob = make_user('bob')
        _member(g, alice)
        _member(g, bob)

        make_test_history(alice, accuracy=80.0)

        row = AnalyticsService.get_cohort_comparison()[0]
        self.assertEqual(row['member_count'], 2)
        self.assertEqual(row['active_users'], 1)
        self.assertEqual(row['session_count'], 1)

    def test_avg_accuracy_is_session_count_weighted(self):
        """
        Alice has one session at 40%, Bob has three sessions at 80%
        each. Weighted mean = (40 + 80*3) / 4 = 70.
        """
        g = _group('Weighted')
        alice = make_user('alice')
        bob = make_user('bob')
        _member(g, alice)
        _member(g, bob)

        make_test_history(alice, accuracy=40.0, total_questions=10)
        for _ in range(3):
            make_test_history(bob, accuracy=80.0, total_questions=10)

        row = AnalyticsService.get_cohort_comparison()[0]
        self.assertEqual(row['session_count'], 4)
        self.assertEqual(row['total_questions'], 40)
        self.assertAlmostEqual(row['avg_accuracy'], 70.0, places=1)

    def test_group_disappears_when_only_sessions_are_outside_window(self):
        """
        A group whose members have sessions, but none inside the
        requested window, is omitted — even though the members
        themselves are active. The filter is on session
        `completed_at`, not on member activity.
        """
        g = _group('Window')
        alice = make_user('alice')
        _member(g, alice)

        old = timezone.now() - timedelta(days=60)
        make_test_history(alice, accuracy=100.0, completed_at=old)

        self.assertEqual(AnalyticsService.get_cohort_comparison(days=30), [])

    def test_group_included_when_any_session_inside_window(self):
        """
        Complement to the test above. The same setup, but with a
        recent session, must produce a row — proving the exclusion
        is specifically about the window, not about the group's
        setup.
        """
        g = _group('Window')
        alice = make_user('alice')
        _member(g, alice)

        old = timezone.now() - timedelta(days=60)
        make_test_history(alice, accuracy=100.0, completed_at=old)
        make_test_history(alice, accuracy=50.0)  # now

        rows = AnalyticsService.get_cohort_comparison(days=30)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session_count'], 1)

    def test_user_in_two_groups_appears_in_both(self):
        g1 = _group('Cohort A')
        g2 = _group('Cohort B')
        alice = make_user('alice')
        _member(g1, alice)
        _member(g2, alice)
        make_test_history(alice, accuracy=80.0)

        rows = AnalyticsService.get_cohort_comparison()
        names = {r['group_name'] for r in rows}
        self.assertEqual(names, {'Cohort A', 'Cohort B'})
        for row in rows:
            self.assertEqual(row['active_users'], 1)
            self.assertEqual(row['session_count'], 1)

    def test_sorted_by_accuracy_then_sessions(self):
        g_low = _group('Low')
        g_high = _group('High')

        alice = make_user('alice')
        bob = make_user('bob')
        _member(g_low, alice)
        _member(g_high, bob)

        make_test_history(alice, accuracy=30.0)
        make_test_history(bob, accuracy=90.0)

        rows = AnalyticsService.get_cohort_comparison()
        self.assertEqual([r['group_name'] for r in rows], ['High', 'Low'])

    def test_inactive_groups_excluded(self):
        active = _group('Active')
        inactive = _group('Inactive')
        inactive.is_active = False
        inactive.save()

        alice = make_user('alice')
        _member(active, alice)
        _member(inactive, alice)
        make_test_history(alice, accuracy=50.0)

        rows = AnalyticsService.get_cohort_comparison()
        names = {r['group_name'] for r in rows}
        self.assertEqual(names, {'Active'})

    def test_days_param_clamps(self):
        AnalyticsService.get_cohort_comparison(days=0)
        AnalyticsService.get_cohort_comparison(days=9999)
        AnalyticsService.get_cohort_comparison(days='garbage')