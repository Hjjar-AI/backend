# tests/api/test_tag_admin_api.py
from rest_framework.test import APIClient

from apps.questions.models import Tag, QuestionTag
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question, make_tag


class TagListAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = make_user('alice')
        self.client.force_login(self.user)

    def test_requires_auth(self):
        self.client.logout()
        resp = self.client.get('/api/v1/questions/tags/')
        self.assertIn(resp.status_code, (401, 403))

    def test_returns_tags_with_counts(self):
        tag = make_tag('alpha')
        q = make_question(owner=self.user)
        q.tags.add(tag)

        resp = self.client.get('/api/v1/questions/tags/')
        self.assertEqual(resp.status_code, 200)
        items = resp.json()['data']['items']
        row = next(i for i in items if i['name'] == 'alpha')
        self.assertEqual(row['count'], 1)
        self.assertEqual(row['verified_count'], 0)

    def test_verified_count_excludes_unverified(self):
        tag = make_tag('beta')
        make_question(owner=self.user, verified=True).tags.add(tag)
        make_question(owner=self.user, verified=False).tags.add(tag)

        resp = self.client.get('/api/v1/questions/tags/')
        row = next(
            i for i in resp.json()['data']['items'] if i['name'] == 'beta'
        )
        self.assertEqual(row['count'], 2)
        self.assertEqual(row['verified_count'], 1)


class AdminTagTreeAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod_user', role='moderator')
        self.member = make_user('member_a')
        self.client.force_login(self.mod)

    def test_member_cannot_access(self):
        self.client.force_login(self.member)
        resp = self.client.get('/api/v1/questions/admin/tags/tree/')
        self.assertEqual(resp.status_code, 403)

    def test_moderator_can_access(self):
        resp = self.client.get('/api/v1/questions/admin/tags/tree/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('tree', resp.json()['data'])

    def test_parent_child_nesting(self):
        parent = make_tag('parent')
        child = make_tag('child')
        child.parent = parent
        child.save()

        resp = self.client.get('/api/v1/questions/admin/tags/tree/')
        tree = resp.json()['data']['tree']
        parent_node = next(n for n in tree if n['name'] == 'parent')
        child_names = [c['name'] for c in parent_node['children']]
        self.assertIn('child', child_names)


class AdminTagRenameAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod_user', role='moderator')
        self.client.force_login(self.mod)

    def test_rename_updates_tag(self):
        make_tag('old-name')
        resp = self.client.post(
            '/api/v1/questions/admin/tags/old-name/rename/',
            {'new_name': 'new-name'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Tag.objects.filter(name='new-name').exists())
        self.assertFalse(Tag.objects.filter(name='old-name').exists())

    def test_rename_to_existing_name_rejected(self):
        make_tag('existing')
        make_tag('target')
        resp = self.client.post(
            '/api/v1/questions/admin/tags/target/rename/',
            {'new_name': 'existing'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_rename_empty_rejected(self):
        make_tag('has-name')
        resp = self.client.post(
            '/api/v1/questions/admin/tags/has-name/rename/',
            {'new_name': '   '},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_rename_overlong_rejected(self):
        make_tag('short')
        resp = self.client.post(
            '/api/v1/questions/admin/tags/short/rename/',
            {'new_name': 'x' * 100},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_rename_nonexistent_returns_404(self):
        resp = self.client.post(
            '/api/v1/questions/admin/tags/nope/rename/',
            {'new_name': 'new'},
            format='json',
        )
        self.assertEqual(resp.status_code, 404)


class AdminTagDeleteAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod_user', role='moderator')
        self.client.force_login(self.mod)

    def test_delete_removes_tag_and_through_rows(self):
        tag = make_tag('gone')
        q = make_question(owner=self.mod)
        q.tags.add(tag)
        self.assertEqual(QuestionTag.objects.filter(tag=tag).count(), 1)

        resp = self.client.delete(
            '/api/v1/questions/admin/tags/gone/delete/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Tag.objects.filter(name='gone').exists())
        self.assertEqual(QuestionTag.objects.filter(tag=tag).count(), 0)

    def test_question_survives_tag_delete(self):
        tag = make_tag('gone-2')
        q = make_question(owner=self.mod)
        q.tags.add(tag)
        qid = q.id

        self.client.delete('/api/v1/questions/admin/tags/gone-2/delete/')

        from apps.questions.models import Question
        self.assertTrue(Question.objects.filter(id=qid).exists())

    def test_delete_nonexistent_returns_404(self):
        resp = self.client.delete(
            '/api/v1/questions/admin/tags/nope/delete/'
        )
        self.assertEqual(resp.status_code, 404)


class AdminTagMergeAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.mod = make_user('mod_user', role='moderator')
        self.client.force_login(self.mod)

    def test_merge_moves_through_rows_and_deletes_sources(self):
        target = make_tag('target')
        src_a = make_tag('src-a')
        src_b = make_tag('src-b')
        q1 = make_question(owner=self.mod)
        q2 = make_question(owner=self.mod)
        q1.tags.add(src_a)
        q2.tags.add(src_b)

        resp = self.client.post(
            '/api/v1/questions/admin/tags/merge/',
            {'source_tags': ['src-a', 'src-b'], 'target_tag': 'target'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Tag.objects.filter(name='src-a').exists())
        self.assertFalse(Tag.objects.filter(name='src-b').exists())
        self.assertTrue(q1.tags.filter(name='target').exists())
        self.assertTrue(q2.tags.filter(name='target').exists())

    def test_merge_creates_target_if_missing(self):
        make_tag('src')
        make_question(owner=self.mod).tags.add(Tag.objects.get(name='src'))

        resp = self.client.post(
            '/api/v1/questions/admin/tags/merge/',
            {'source_tags': ['src'], 'target_tag': 'brand-new'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Tag.objects.filter(name='brand-new').exists())

    def test_merge_dedupes_pairs_when_question_has_both(self):
        target = make_tag('target')
        src = make_tag('src')
        q = make_question(owner=self.mod)
        q.tags.add(target)
        q.tags.add(src)

        self.client.post(
            '/api/v1/questions/admin/tags/merge/',
            {'source_tags': ['src'], 'target_tag': 'target'},
            format='json',
        )
        # Question ends up with exactly one 'target' row.
        self.assertEqual(q.tags.filter(name='target').count(), 1)

    def test_merge_empty_sources_rejected(self):
        resp = self.client.post(
            '/api/v1/questions/admin/tags/merge/',
            {'source_tags': [], 'target_tag': 'x'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_merge_empty_target_rejected(self):
        resp = self.client.post(
            '/api/v1/questions/admin/tags/merge/',
            {'source_tags': ['a'], 'target_tag': ''},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_source_equal_to_target_is_skipped(self):
        t = make_tag('same')
        q = make_question(owner=self.mod)
        q.tags.add(t)

        resp = self.client.post(
            '/api/v1/questions/admin/tags/merge/',
            {'source_tags': ['same'], 'target_tag': 'same'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Tag.objects.filter(name='same').exists())

    def test_member_cannot_merge(self):
        make_tag('a')
        make_tag('b')
        self.client.force_login(make_user('member_a'))
        resp = self.client.post(
            '/api/v1/questions/admin/tags/merge/',
            {'source_tags': ['a'], 'target_tag': 'b'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)