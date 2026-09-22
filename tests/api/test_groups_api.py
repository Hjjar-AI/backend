# tests/api/test_groups_api.py
from rest_framework.test import APIClient

from apps.groups.models import Group, GroupMembership
from apps.groups.services import GroupService
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_test_history


class MyGroupsAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.other = make_user('bob')
        self.client.force_login(self.u)

    def test_lists_only_memberships(self):
        g1 = GroupService.create_group('Mine', '', 'admin')
        g2 = GroupService.create_group('Theirs', '', 'admin')
        GroupService.add_members(g1, [self.u.id])
        GroupService.add_members(g2, [self.other.id])

        resp = self.client.get('/api/v1/study/groups/mine/')
        self.assertEqual(resp.status_code, 200)
        names = {g['name'] for g in resp.json()['data']['items']}
        self.assertEqual(names, {'Mine'})

    def test_empty_when_no_memberships(self):
        resp = self.client.get('/api/v1/study/groups/mine/')
        self.assertEqual(resp.json()['data']['items'], [])


class GroupLeaderboardAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.member = make_user('member')
        self.outsider = make_user('outsider')
        self.admin = make_user('mod_user', role='moderator')
        self.g = GroupService.create_group('Cohort', '', 'admin')
        GroupService.add_members(self.g, [self.member.id])

    def test_member_can_view(self):
        self.client.force_login(self.member)
        resp = self.client.get(
            f'/api/v1/study/groups/{self.g.id}/leaderboard/'
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertIn('group', data)
        self.assertIn('rows', data)
        self.assertEqual(data['days'], 7)

    def test_outsider_gets_404_not_403(self):
        """
        The view returns the same 404 as a nonexistent group so
        membership cannot be probed by observing 403-vs-404.
        """
        self.client.force_login(self.outsider)
        resp = self.client.get(
            f'/api/v1/study/groups/{self.g.id}/leaderboard/'
        )
        self.assertEqual(resp.status_code, 404)

    def test_outsider_nonexistent_group_same_response(self):
        self.client.force_login(self.outsider)
        resp = self.client.get(
            '/api/v1/study/groups/999999/leaderboard/'
        )
        self.assertEqual(resp.status_code, 404)

    def test_moderator_with_groups_admin_can_view_any(self):
        self.client.force_login(self.admin)
        resp = self.client.get(
            f'/api/v1/study/groups/{self.g.id}/leaderboard/'
        )
        self.assertEqual(resp.status_code, 200)

    def test_days_param_clamped(self):
        self.client.force_login(self.member)
        resp = self.client.get(
            f'/api/v1/study/groups/{self.g.id}/leaderboard/?days=9999'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['days'], 90)

    def test_invalid_days_param_uses_default(self):
        self.client.force_login(self.member)
        resp = self.client.get(
            f'/api/v1/study/groups/{self.g.id}/leaderboard/?days=garbage'
        )
        self.assertEqual(resp.json()['data']['days'], 7)

    def test_leaderboard_data_reflects_test_history(self):
        make_test_history(
            self.member, total_questions=30, correct_count=21,
        )
        self.client.force_login(self.member)
        resp = self.client.get(
            f'/api/v1/study/groups/{self.g.id}/leaderboard/'
        )
        rows = resp.json()['data']['rows']
        member_row = next(r for r in rows if r['username'] == 'member')
        self.assertEqual(member_row['questions_answered'], 30)
        self.assertEqual(member_row['accuracy'], 70.0)


class GroupVisibilityAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.g = GroupService.create_group('Cohort', '', 'admin')
        GroupService.add_members(self.g, [self.u.id])
        self.client.force_login(self.u)

    def test_toggle_visibility_off(self):
        resp = self.client.post(
            f'/api/v1/study/groups/{self.g.id}/visibility/',
            {'show_in_leaderboard': False},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        m = GroupMembership.objects.get(group=self.g, user=self.u)
        self.assertFalse(m.show_in_leaderboard)

    def test_non_member_returns_404(self):
        outsider = make_user('outsider')
        self.client.force_login(outsider)
        resp = self.client.post(
            f'/api/v1/study/groups/{self.g.id}/visibility/',
            {'show_in_leaderboard': True},
            format='json',
        )
        self.assertEqual(resp.status_code, 404)


class AdminGroupAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod_user', role='moderator')
        self.member = make_user('member_a')
        self.client.force_login(self.mod)

    def test_member_cannot_access_admin_endpoints(self):
        self.client.force_login(self.member)
        resp = self.client.get('/api/v1/study/admin/groups/')
        self.assertEqual(resp.status_code, 403)

    def test_create_group(self):
        resp = self.client.post(
            '/api/v1/study/admin/groups/',
            {'name': 'New Group', 'description': 'desc'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(Group.objects.filter(name='New Group').exists())

    def test_duplicate_name_rejected(self):
        GroupService.create_group('Existing', '', 'mod_user')
        resp = self.client.post(
            '/api/v1/study/admin/groups/',
            {'name': 'Existing'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_update_group(self):
        g = GroupService.create_group('Old', '', 'mod_user')
        resp = self.client.put(
            f'/api/v1/study/admin/groups/{g.id}/',
            {'name': 'New'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        g.refresh_from_db()
        self.assertEqual(g.name, 'New')

    def test_update_to_existing_name_rejected(self):
        GroupService.create_group('Taken', '', 'mod_user')
        g = GroupService.create_group('Mine', '', 'mod_user')
        resp = self.client.put(
            f'/api/v1/study/admin/groups/{g.id}/',
            {'name': 'Taken'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_group(self):
        g = GroupService.create_group('Gone', '', 'mod_user')
        resp = self.client.delete(f'/api/v1/study/admin/groups/{g.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Group.objects.filter(id=g.id).exists())

    def test_bulk_add_members_reports_counts(self):
        g = GroupService.create_group('Team', '', 'mod_user')
        # Usernames must satisfy USERNAME_REGEX: 3-50 chars,
        # letters/digits/underscore only.
        u1 = make_user('usr_a')
        u2 = make_user('usr_b')
        resp = self.client.post(
            f'/api/v1/study/admin/groups/{g.id}/members/',
            {'user_ids': [u1.id, u2.id, 999999]},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(data['added'], 2)
        self.assertEqual(data['requested'], 3)

    def test_add_members_all_invalid(self):
        g = GroupService.create_group('Team', '', 'mod_user')
        resp = self.client.post(
            f'/api/v1/study/admin/groups/{g.id}/members/',
            {'user_ids': [999998, 999999]},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_remove_member(self):
        g = GroupService.create_group('Team', '', 'mod_user')
        u = make_user('usr_a')
        GroupService.add_members(g, [u.id])
        resp = self.client.delete(
            f'/api/v1/study/admin/groups/{g.id}/members/{u.id}/'
        )
        self.assertEqual(resp.status_code, 200)

    def test_remove_nonexistent_member_returns_404(self):
        g = GroupService.create_group('Team', '', 'mod_user')
        u = make_user('usr_a')
        resp = self.client.delete(
            f'/api/v1/study/admin/groups/{g.id}/members/{u.id}/'
        )
        self.assertEqual(resp.status_code, 404)