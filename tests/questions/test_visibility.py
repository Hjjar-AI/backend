# tests/questions/test_visibility.py
from django.contrib.auth.models import AnonymousUser

from apps.questions.models import Question
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


class QuestionVisibilityTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.alice = make_user('alice')
        self.bob = make_user('bob')

        self.public = make_question(owner=self.alice, question='public?')
        self.alice_draft = make_question(
            owner=self.alice,
            is_draft=True,
            draft_owner=self.alice,
            question='alice draft?',
        )
        self.bob_draft = make_question(
            owner=self.bob,
            is_draft=True,
            draft_owner=self.bob,
            question='bob draft?',
        )

    def test_public_excludes_drafts(self):
        ids = set(Question.objects.public().values_list('id', flat=True))
        self.assertIn(self.public.id, ids)
        self.assertNotIn(self.alice_draft.id, ids)
        self.assertNotIn(self.bob_draft.id, ids)

    def test_visible_to_anonymous_returns_public_only(self):
        ids = set(
            Question.objects.visible_to(AnonymousUser())
            .values_list('id', flat=True)
        )
        self.assertEqual(ids, {self.public.id})

    def test_visible_to_none_returns_public_only(self):
        ids = set(
            Question.objects.visible_to(None).values_list('id', flat=True)
        )
        self.assertEqual(ids, {self.public.id})

    def test_visible_to_member_sees_public_plus_own_drafts(self):
        ids = set(
            Question.objects.visible_to(self.alice)
            .values_list('id', flat=True)
        )
        self.assertEqual(ids, {self.public.id, self.alice_draft.id})

    def test_drafts_for_returns_own_drafts_only(self):
        ids = set(
            Question.objects.drafts_for(self.alice)
            .values_list('id', flat=True)
        )
        self.assertEqual(ids, {self.alice_draft.id})

    def test_drafts_for_anonymous_returns_empty(self):
        self.assertEqual(
            Question.objects.drafts_for(AnonymousUser()).count(), 0,
        )


class CheckConstraintTests(CacheClearingTestCase):
    def test_published_question_requires_owner(self):
        """
        The model-level CheckConstraint refuses a published question
        with owned_by=NULL. On MariaDB the constraint is enforced;
        on a backend that does not enforce it (SQLite in older
        versions) the test would silently pass — so this test also
        asserts the constraint is declared on the model metadata.
        """
        constraint_names = {
            c.name for c in Question._meta.constraints
        }
        self.assertIn('question_published_has_owner', constraint_names)

        user = make_user()
        with self.assertRaises(Exception):
            Question.objects.create(
                question='no owner?',
                choices=['A', 'B'],
                correct_answer=1,
                is_draft=False,
                authored_by=user,
                owned_by=None,
            )