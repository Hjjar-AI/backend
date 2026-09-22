# tests/groups/test_group_service.py
from datetime import timedelta

from django.utils import timezone

from apps.groups.models import Group, GroupMembership
from apps.groups.services import GroupService, LeaderboardService
from tests.base import CacheClearingTestCase
from tests.factories import (
    make_user, make_test_history,
)


class GroupServiceTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.creator = make_user('creator')

    def test_create_group_strips_and_normalizes(self):
        g = GroupService.create_group(
            '  Alpha Group  ', '   ', 'creator',
        )
        self.assertEqual(g.name, 'Alpha Group')
        self.assertIsNone(g.description)

    def test_list_groups_excludes_inactive_by_default(self):
        GroupService.create_group('Active', '', 'creator')
        g2 = GroupService.create_group('Inactive', '', 'creator')
        g2.is_active = False
        g2.save()
        names = list(
            GroupService.list_groups().values_list('name', flat=True)
        )
        self.assertIn('Active', names)
        self.assertNotIn('Inactive', names)

    def test_list_groups_include_inactive_flag(self):
        g = GroupService.create_group('Hid', '', 'creator')
        g.is_active = False
        g.save()
        names = list(
            GroupService.list_groups(include_inactive=True)
            .values_list('name', flat=True)
        )
        self.assertIn('Hid', names)

    def test_add_members_deduplicates_and_skips_existing(self):
        g = GroupService.create_group('Team', '', 'creator')
        u1 = make_user('usr_a')
        u2 = make_user('usr_b')

        added = GroupService.add_members(g, [u1.id, u2.id, u1.id, u2.id])
        self.assertEqual(added, 2)
        self.assertEqual(g.memberships.count(), 2)

        added = GroupService.add_members(g, [u1.id, u2.id])
        self.assertEqual(added, 0)

    def test_add_members_empty_list(self):
        g = GroupService.create_group('Empty', '', 'creator')
        self.assertEqual(GroupService.add_members(g, []), 0)

    def test_remove_member(self):
        g = GroupService.create_group('Team', '', 'creator')
        u = make_user('usr_a')
        GroupService.add_members(g, [u.id])
        self.assertTrue(GroupService.remove_member(g, u.id))
        self.assertFalse(GroupService.remove_member(g, u.id))

    def test_set_visibility(self):
        g = GroupService.create_group('Team', '', 'creator')
        u = make_user('usr_a')
        GroupService.add_members(g, [u.id])
        m = GroupMembership.objects.get(group=g, user=u)
        self.assertTrue(m.show_in_leaderboard)

        self.assertTrue(GroupService.set_visibility(g, u.id, False))
        m.refresh_from_db()
        self.assertFalse(m.show_in_leaderboard)

    def test_set_visibility_non_member_returns_false(self):
        g = GroupService.create_group('Team', '', 'creator')
        u = make_user('usr_a')
        self.assertFalse(GroupService.set_visibility(g, u.id, False))

    def test_groups_for_user_only_returns_own_active_memberships(self):
        u1 = make_user('usr_a')
        u2 = make_user('usr_b')
        g1 = GroupService.create_group('Mine', '', 'creator')
        g2 = GroupService.create_group('Theirs', '', 'creator')
        g3 = GroupService.create_group('Inactive', '', 'creator')
        g3.is_active = False
        g3.save()

        GroupService.add_members(g1, [u1.id])
        GroupService.add_members(g2, [u2.id])
        GroupService.add_members(g3, [u1.id])

        names = set(
            GroupService.groups_for_user(u1).values_list('name', flat=True)
        )
        self.assertEqual(names, {'Mine'})

    def test_update_group_partial(self):
        g = GroupService.create_group('Old', 'desc', 'creator')
        GroupService.update_group(g, name='New')
        g.refresh_from_db()
        self.assertEqual(g.name, 'New')
        self.assertEqual(g.description, 'desc')

    def test_update_group_clears_description_when_blank(self):
        g = GroupService.create_group('Name', 'has desc', 'creator')
        GroupService.update_group(g, description='   ')
        g.refresh_from_db()
        self.assertIsNone(g.description)

    def test_delete_group(self):
        g = GroupService.create_group('Gone', '', 'creator')
        gid = g.id
        GroupService.delete_group(g)
        self.assertFalse(Group.objects.filter(id=gid).exists())


class LeaderboardServiceTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.g = GroupService.create_group('Cohort', '', 'creator')
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.carol = make_user('carol')
        for u in (self.alice, self.bob, self.carol):
            GroupService.add_members(self.g, [u.id])

    def test_empty_group_returns_empty(self):
        g = GroupService.create_group('Empty', '', 'creator')
        self.assertEqual(LeaderboardService.group_leaderboard(g), [])

    def test_ranks_by_questions_answered(self):
        make_test_history(
            self.alice, total_questions=10, correct_count=5,
        )
        make_test_history(
            self.bob, total_questions=50, correct_count=40,
        )
        make_test_history(
            self.carol, total_questions=20, correct_count=15,
        )
        rows = LeaderboardService.group_leaderboard(self.g)
        usernames = [r['username'] for r in rows]
        self.assertEqual(usernames, ['bob', 'carol', 'alice'])
        self.assertEqual(rows[0]['rank'], 1)
        self.assertEqual(rows[1]['rank'], 2)
        self.assertEqual(rows[2]['rank'], 3)

    def test_members_with_show_in_leaderboard_false_are_hidden(self):
        make_test_history(self.alice, total_questions=100, correct_count=90)
        make_test_history(self.bob, total_questions=5, correct_count=1)
        GroupService.set_visibility(self.g, self.alice.id, False)
        rows = LeaderboardService.group_leaderboard(self.g)
        usernames = [r['username'] for r in rows]
        self.assertNotIn('alice', usernames)
        self.assertIn('bob', usernames)

    def test_window_excludes_old_sessions(self):
        make_test_history(
            self.alice, total_questions=10, correct_count=5,
        )
        make_test_history(
            self.bob, total_questions=100, correct_count=50,
            completed_at=timezone.now() - timedelta(days=30),
        )
        rows = LeaderboardService.group_leaderboard(self.g, days=7)
        bob_row = next(r for r in rows if r['username'] == 'bob')
        self.assertEqual(bob_row['questions_answered'], 0)
        self.assertEqual(bob_row['sessions'], 0)

    def test_accuracy_is_correct_over_answered(self):
        make_test_history(
            self.alice, total_questions=10, correct_count=7,
        )
        make_test_history(
            self.alice, total_questions=10, correct_count=3,
        )
        rows = LeaderboardService.group_leaderboard(self.g)
        alice_row = next(r for r in rows if r['username'] == 'alice')
        self.assertEqual(alice_row['questions_answered'], 20)
        self.assertEqual(alice_row['correct_count'], 10)
        self.assertEqual(alice_row['accuracy'], 50.0)

    def test_streak_fields_carried_through(self):
        self.alice.current_streak = 5
        self.alice.longest_streak = 12
        self.alice.save()
        rows = LeaderboardService.group_leaderboard(self.g)
        alice_row = next(r for r in rows if r['username'] == 'alice')
        self.assertEqual(alice_row['current_streak'], 5)
        self.assertEqual(alice_row['longest_streak'], 12)