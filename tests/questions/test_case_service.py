# tests/questions/test_case_service.py
from apps.questions.models import ClinicalCase
from apps.questions.services import QuestionService
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question, make_case, make_category


class UpdateCaseStemServiceTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.moderator = make_user('mod1', role='moderator')
        self.author = make_user('author')
        self.other = make_user('other')

        self.case = make_case('shared-case')
        self.q1 = make_question(owner=self.author, case=self.case)
        self.q2 = make_question(owner=self.author, case=self.case)

    def test_empty_key_returns_zero(self):
        updated = QuestionService.update_case_stem(
            '', 'new stem', self.moderator,
        )
        self.assertEqual(updated, 0)

    def test_missing_case_returns_zero(self):
        updated = QuestionService.update_case_stem(
            'no-such-case', 'new stem', self.moderator,
        )
        self.assertEqual(updated, 0)

    def test_moderator_with_edit_any_can_update(self):
        updated = QuestionService.update_case_stem(
            'shared-case', 'new stem text', self.moderator,
        )
        self.assertEqual(updated, 2)
        self.case.refresh_from_db()
        self.assertEqual(self.case.stem, 'new stem text')

    def test_author_with_own_capability_can_update(self):
        # Author has questions in this case AND holds
        # 'questions.edit_case_stem_own' (which every member has).
        updated = QuestionService.update_case_stem(
            'shared-case', 'author stem', self.author,
        )
        self.assertEqual(updated, 2)

    def test_non_author_member_raises_permission_error(self):
        with self.assertRaises(PermissionError):
            QuestionService.update_case_stem(
                'shared-case', 'hijacked', self.other,
            )

    def test_author_without_own_capability_raises_permission_error(self):
        # Revoke the capability via override.
        self.author.capabilities = {'questions.edit_case_stem_own': False}
        self.author.save()
        with self.assertRaises(PermissionError):
            QuestionService.update_case_stem(
                'shared-case', 'hijacked', self.author,
            )

    def test_blank_stem_normalizes_to_none(self):
        self.case.stem = 'existing'
        self.case.save()
        QuestionService.update_case_stem(
            'shared-case', '   ', self.moderator,
        )
        self.case.refresh_from_db()
        self.assertIsNone(self.case.stem)

    def test_explicit_none_stem_normalizes_to_none(self):
        QuestionService.update_case_stem(
            'shared-case', None, self.moderator,
        )
        self.case.refresh_from_db()
        self.assertIsNone(self.case.stem)


class ResolveCaseHelperTests(CacheClearingTestCase):
    """
    _resolve_case is the shared write-path helper used by every
    serializer that can attach a question to a case.
    """
    def setUp(self):
        super().setUp()
        self.user = make_user('author')

    def test_empty_key_returns_none(self):
        from apps.questions.serializers import _resolve_case
        self.assertIsNone(_resolve_case(None, self.user))
        self.assertIsNone(_resolve_case('', self.user))
        self.assertIsNone(_resolve_case('   ', self.user))

    def test_creates_case_with_stem_and_author(self):
        from apps.questions.serializers import _resolve_case
        case = _resolve_case(
            'new-case', self.user, stem='vignette text',
        )
        self.assertEqual(case.key, 'new-case')
        self.assertEqual(case.stem, 'vignette text')
        self.assertEqual(case.authored_by, self.user)

    def test_reuses_existing_case(self):
        from apps.questions.serializers import _resolve_case
        existing = make_case('existing-case')
        case = _resolve_case('existing-case', self.user)
        self.assertEqual(case.pk, existing.pk)

    def test_stem_fills_when_target_has_none(self):
        from apps.questions.serializers import _resolve_case
        c = make_case('no-stem')
        _resolve_case('no-stem', self.user, stem='filled in')
        c.refresh_from_db()
        self.assertEqual(c.stem, 'filled in')

    def test_stem_is_not_overwritten_on_populated_case(self):
        from apps.questions.serializers import _resolve_case
        c = make_case('has-stem', stem='original')
        _resolve_case('has-stem', self.user, stem='replacement')
        c.refresh_from_db()
        self.assertEqual(c.stem, 'original')

    def test_existing_case_fast_path(self):
        from apps.questions.serializers import _resolve_case
        existing = make_case('my-case')
        result = _resolve_case(
            'my-case', self.user, existing_case=existing,
        )
        self.assertEqual(result.pk, existing.pk)