# tests/api/test_cases_api.py
from rest_framework.test import APIClient

from apps.questions.models import ClinicalCase, Question
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question, make_case


class CaseListAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.alice = make_user('alice')
        self.bob = make_user('bob')

    def test_public_case_visible_to_everyone(self):
        case = make_case('public-case')
        make_question(owner=self.bob, case=case)

        self.client.force_login(self.alice)
        resp = self.client.get('/api/v1/questions/cases/')
        self.assertEqual(resp.status_code, 200)
        keys = {c['key'] for c in resp.json()['data']['items']}
        self.assertIn('public-case', keys)

    def test_own_draft_case_visible_to_owner(self):
        case = make_case('alice-draft-case')
        make_question(
            owner=self.alice, case=case,
            is_draft=True, draft_owner=self.alice,
        )
        self.client.force_login(self.alice)
        resp = self.client.get('/api/v1/questions/cases/')
        keys = {c['key'] for c in resp.json()['data']['items']}
        self.assertIn('alice-draft-case', keys)

    def test_other_users_draft_case_is_hidden(self):
        case = make_case('bob-draft-case')
        make_question(
            owner=self.bob, case=case,
            is_draft=True, draft_owner=self.bob,
        )
        self.client.force_login(self.alice)
        resp = self.client.get('/api/v1/questions/cases/')
        keys = {c['key'] for c in resp.json()['data']['items']}
        self.assertNotIn('bob-draft-case', keys)

    def test_moderator_sees_all_cases(self):
        case = make_case('bob-draft-case-2')
        make_question(
            owner=self.bob, case=case,
            is_draft=True, draft_owner=self.bob,
        )
        mod = make_user('mod_user', role='moderator')
        self.client.force_login(mod)
        resp = self.client.get('/api/v1/questions/cases/')
        keys = {c['key'] for c in resp.json()['data']['items']}
        self.assertIn('bob-draft-case-2', keys)

    def test_search_by_key_or_title(self):
        """
        Visibility of a case is derived from the questions that
        reference it (or a direct `authored_by` on the case). Cases
        with no questions attached are invisible to a plain member.
        Attach one question per case so the search has something to
        match against.
        """
        c1 = make_case('depp-case', title='Depression vignette')
        c2 = make_case('anxiety-case', title='Anxiety vignette')
        make_question(owner=self.alice, case=c1)
        make_question(owner=self.alice, case=c2)

        self.client.force_login(self.alice)
        resp = self.client.get('/api/v1/questions/cases/?search=dep')
        keys = {c['key'] for c in resp.json()['data']['items']}
        self.assertIn('depp-case', keys)
        self.assertNotIn('anxiety-case', keys)

    def test_limit_clamped_to_100(self):
        for i in range(150):
            c = make_case(f'c{i:03d}')
            make_question(owner=self.alice, case=c)
        self.client.force_login(self.alice)
        resp = self.client.get('/api/v1/questions/cases/?limit=9999')
        self.assertLessEqual(len(resp.json()['data']['items']), 100)


class CaseDetailAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.case = make_case('shared', stem='initial stem')
        make_question(owner=self.alice, case=self.case, question='q1')
        make_question(owner=self.alice, case=self.case, question='q2')

    def test_get_returns_questions_list(self):
        self.client.force_login(self.alice)
        resp = self.client.get('/api/v1/questions/cases/shared/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(len(data['questions']), 2)
        self.assertEqual(data['key'], 'shared')

    def test_get_404_for_invisible_case(self):
        bob_case = make_case('bob-only')
        make_question(
            owner=self.bob, case=bob_case,
            is_draft=True, draft_owner=self.bob,
        )
        self.client.force_login(self.alice)
        resp = self.client.get('/api/v1/questions/cases/bob-only/')
        self.assertEqual(resp.status_code, 404)

    def test_put_title_by_author(self):
        self.client.force_login(self.alice)
        resp = self.client.put(
            '/api/v1/questions/cases/shared/',
            {'title': 'Renamed'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.case.refresh_from_db()
        self.assertEqual(self.case.title, 'Renamed')

    def test_put_stem_by_author(self):
        self.client.force_login(self.alice)
        resp = self.client.put(
            '/api/v1/questions/cases/shared/',
            {'stem': 'updated stem'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.case.refresh_from_db()
        self.assertEqual(self.case.stem, 'updated stem')

    def test_put_refused_for_non_author(self):
        """
        The case is visible to bob — alice's questions in it are
        public. So the lookup succeeds and the refusal comes from
        the edit-capability check, not from the visibility filter.
        The correct status is 403, not 404.
        """
        self.client.force_login(self.bob)
        resp = self.client.put(
            '/api/v1/questions/cases/shared/',
            {'title': 'hijack'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_delete_requires_edit_case_stem_any(self):
        self.client.force_login(self.alice)
        resp = self.client.delete('/api/v1/questions/cases/shared/')
        self.assertEqual(resp.status_code, 403)

    def test_moderator_can_delete_case_and_questions_survive(self):
        mod = make_user('mod_user', role='moderator')
        self.client.force_login(mod)
        q_count = self.case.questions.count()
        resp = self.client.delete('/api/v1/questions/cases/shared/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['detached_questions'], q_count)
        self.assertFalse(ClinicalCase.objects.filter(key='shared').exists())
        self.assertEqual(
            Question.objects.filter(
                question__in=['q1', 'q2'], case_id__isnull=True,
            ).count(),
            q_count,
        )


class CaseStemUpdateAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.case = make_case('shared-stem')
        make_question(owner=self.alice, case=self.case)

    def test_member_not_author_gets_403(self):
        """
        The case is visible to bob (alice's question in it is
        public), so the pre-filter passes and the service raises
        PermissionError — mapped to 403 by the view.
        """
        self.client.force_login(self.bob)
        resp = self.client.post(
            '/api/v1/questions/case/shared-stem/stem/',
            {'case_stem': 'hijack'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_author_can_update_stem(self):
        self.client.force_login(self.alice)
        resp = self.client.post(
            '/api/v1/questions/case/shared-stem/stem/',
            {'case_stem': 'author edit'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.case.refresh_from_db()
        self.assertEqual(self.case.stem, 'author edit')

    def test_nonexistent_case_returns_404(self):
        self.client.force_login(self.alice)
        resp = self.client.post(
            '/api/v1/questions/case/nope/stem/',
            {'case_stem': 'x'},
            format='json',
        )
        self.assertEqual(resp.status_code, 404)

    def test_blank_stem_normalized_to_null(self):
        self.client.force_login(self.alice)
        self.client.post(
            '/api/v1/questions/case/shared-stem/stem/',
            {'case_stem': '   '},
            format='json',
        )
        self.case.refresh_from_db()
        self.assertIsNone(self.case.stem)