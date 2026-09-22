# tests/questions/test_question_service.py
from apps.questions.models import Question, Tag
from apps.questions.services import QuestionService
from tests.base import CacheClearingTestCase
from tests.factories import (
    make_user, make_admin, make_question, make_category, make_tag,
)


class GetQuestionsFiltersTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.cat_a = make_category('cat-a')
        self.cat_b = make_category('cat-b')
        self.tag_x = make_tag('tag-x')
        self.tag_y = make_tag('tag-y')

        self.q_a = make_question(
            owner=self.user, category=self.cat_a, difficulty='easy',
        )
        self.q_a.tags.add(self.tag_x)

        self.q_b = make_question(
            owner=self.user, category=self.cat_b, difficulty='hard',
        )
        self.q_b.tags.add(self.tag_y)

        self.q_c = make_question(
            owner=self.user, category=self.cat_a, difficulty='medium',
            verified=True,
        )

    def test_category_ids_filter(self):
        qs = QuestionService.get_questions(
            {'category_ids': [self.cat_a.id]}, user=None,
        )
        self.assertEqual(
            set(qs.values_list('id', flat=True)),
            {self.q_a.id, self.q_c.id},
        )

    def test_single_category_filter_normalizes_to_list(self):
        qs = QuestionService.get_questions(
            {'category': str(self.cat_b.id)}, user=None,
        )
        self.assertEqual(
            set(qs.values_list('id', flat=True)), {self.q_b.id},
        )

    def test_category_ids_takes_precedence_over_category(self):
        """
        When both are present, the list wins. The single-value branch
        is a legacy path for old clients, and ANDing them would be a
        surprise.
        """
        qs = QuestionService.get_questions(
            {'category_ids': [self.cat_a.id], 'category': str(self.cat_b.id)},
            user=None,
        )
        self.assertEqual(
            set(qs.values_list('id', flat=True)),
            {self.q_a.id, self.q_c.id},
        )

    def test_difficulty_filter(self):
        qs = QuestionService.get_questions({'difficulty': 'easy'}, user=None)
        self.assertEqual(
            set(qs.values_list('id', flat=True)), {self.q_a.id},
        )

    def test_verified_filter_yes(self):
        qs = QuestionService.get_questions({'verified': 'yes'}, user=None)
        self.assertEqual(
            set(qs.values_list('id', flat=True)), {self.q_c.id},
        )

    def test_search_requires_two_chars(self):
        make_question(owner=self.user, question='uniquephrase')
        qs = QuestionService.get_questions({'search': 'u'}, user=None)
        # One char — no filtering.
        self.assertGreater(qs.count(), 1)
        qs = QuestionService.get_questions({'search': 'uniquephrase'}, user=None)
        self.assertEqual(qs.count(), 1)

    def test_tags_filter_or_within_set(self):
        qs = QuestionService.get_questions(
            {'tags_filter': ['tag-x', 'tag-y']}, user=None,
        )
        self.assertEqual(
            set(qs.values_list('id', flat=True)),
            {self.q_a.id, self.q_b.id},
        )


class CreateQuestionTests(CacheClearingTestCase):
    def test_create_sets_authored_and_owned_to_caller(self):
        user = make_user()
        q = QuestionService.create_question(
            {
                'question': 'New question?',
                'choices': ['A', 'B'],
                'correct_answer': 1,
            },
            user,
        )
        self.assertEqual(q.authored_by, user)
        self.assertEqual(q.owned_by, user)

    def test_create_attributes_case_to_author(self):
        user = make_user()
        q = QuestionService.create_question(
            {
                'question': 'With a case?',
                'choices': ['A', 'B'],
                'correct_answer': 1,
                'case_key': 'case-alpha',
                'case_stem': 'A 42-year-old man…',
            },
            user,
        )
        self.assertIsNotNone(q.case_id)
        self.assertEqual(q.case.key, 'case-alpha')
        self.assertEqual(q.case.authored_by, user)
        self.assertEqual(q.case.stem, 'A 42-year-old man…')

    def test_create_updates_author_trust_score(self):
        user = make_user()
        self.assertEqual(user.questions_count, 0)
        QuestionService.create_question(
            {
                'question': 'Count me?',
                'choices': ['A', 'B'],
                'correct_answer': 1,
            },
            user,
        )
        user.refresh_from_db()
        self.assertEqual(user.questions_count, 1)


class VersionCASTests(CacheClearingTestCase):
    def test_stale_expected_version_raises(self):
        user = make_user()
        q = make_question(owner=user)

        # Bump the version by editing once.
        QuestionService.update_question(
            q.id,
            {
                'question': 'Edited once?',
                'expected_version': q.version,
            },
            user,
        )
        q.refresh_from_db()

        # Now attempt an edit with the OLD version.
        with self.assertRaises(ValueError):
            QuestionService.update_question(
                q.id,
                {
                    'question': 'Stale edit?',
                    'expected_version': q.version - 1,
                },
                user,
            )

    def test_matching_expected_version_succeeds(self):
        user = make_user()
        q = make_question(owner=user)
        QuestionService.update_question(
            q.id,
            {'question': 'ok?', 'expected_version': q.version},
            user,
        )
        q.refresh_from_db()
        self.assertEqual(q.question, 'ok?')


class BulkVerifyTests(CacheClearingTestCase):
    def test_bulk_verify_sets_fields_and_recomputes_trust(self):
        author = make_user()
        q1 = make_question(owner=author)
        q2 = make_question(owner=author)

        count = QuestionService.bulk_verify(
            [q1.id, q2.id], 'moderator', 'looks good',
        )
        self.assertEqual(count, 2)

        q1.refresh_from_db()
        self.assertTrue(q1.verified)
        self.assertEqual(q1.verified_by, 'moderator')
        self.assertEqual(q1.verification_notes, 'looks good')

        author.refresh_from_db()
        self.assertEqual(author.questions_count, 2)
        self.assertEqual(author.trust_score, 100.0)

    def test_bulk_unverify_resets_counters(self):
        author = make_user()
        q = make_question(owner=author, verified=True, verified_by='x')
        QuestionService.bulk_unverify([q.id])

        q.refresh_from_db()
        self.assertFalse(q.verified)
        self.assertIsNone(q.verified_by)

        author.refresh_from_db()
        self.assertEqual(author.trust_score, 0.0)


class BulkUpdateTagsTests(CacheClearingTestCase):
    def test_add_and_remove(self):
        user = make_user()
        q1 = make_question(owner=user)
        q2 = make_question(owner=user)
        old_tag = make_tag('old-tag')
        q1.tags.add(old_tag)
        q2.tags.add(old_tag)

        count = QuestionService.bulk_update_tags(
            [q1.id, q2.id],
            add_tags=['new-tag', 'new-tag'],   # duplicate on purpose
            remove_tags=['old-tag'],
        )
        self.assertEqual(count, 2)

        for q in (q1, q2):
            q.refresh_from_db()
            names = set(q.tags.values_list('name', flat=True))
            self.assertIn('new-tag', names)
            self.assertNotIn('old-tag', names)

    def test_non_string_remove_tag_is_skipped(self):
        """
        The serializer enforces strings at the API boundary. This
        guard is defensive — a plain int must not crash and must not
        be silently coerced to a lookup for a tag named "123".
        """
        user = make_user()
        q = make_question(owner=user)
        make_tag('123')
        QuestionService.bulk_update_tags(
            [q.id], add_tags=[], remove_tags=[123, None, 'real-tag'],
        )
        q.refresh_from_db()
        names = set(q.tags.values_list('name', flat=True))
        # '123' tag was NOT removed (it was never added) and did not
        # crash the loop.
        self.assertNotIn('real-tag', names)