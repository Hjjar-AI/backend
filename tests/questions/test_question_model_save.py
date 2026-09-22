# tests/questions/test_question_model_save.py
"""
`Question.save()` range-checks `correct_answer` against `choices`.

The serializer layer enforces this everywhere it can, but the
model-level guard is what protects writes that bypass serializers
— Django admin, management commands, scripts. The tests below pin
the guard and document the paths that bypass it.
"""
from apps.questions.models import Question
from tests.base import CacheClearingTestCase
from tests.factories import make_user


class QuestionSaveValidationTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('alice')

    def _make(self, **kw):
        defaults = {
            'question': 'X?',
            'choices': ['A', 'B'],
            'correct_answer': 1,
            'owned_by': self.user,
            'authored_by': self.user,
        }
        defaults.update(kw)
        return Question.objects.create(**defaults)

    def test_in_range_correct_answer_saves(self):
        q = self._make(correct_answer=2)
        self.assertEqual(q.correct_answer, 2)

    def test_correct_answer_above_range_rejected(self):
        with self.assertRaises(ValueError):
            self._make(correct_answer=99)

    def test_correct_answer_below_range_rejected(self):
        with self.assertRaises(ValueError):
            self._make(correct_answer=0)

    def test_empty_choices_skips_the_range_check(self):
        """
        `choices` is a JSONField list. When it is empty, the model's
        guard is a no-op — the check cannot decide a range. The
        CheckConstraint on the model does not cover this either,
        because the range depends on the list length.
        """
        q = self._make(choices=[], correct_answer=1)
        self.assertEqual(q.choices, [])

    def test_bulk_create_bypasses_the_save_guard(self):
        """
        Documents the asymmetry: `save()` validates, `bulk_create()`
        does not. Any code path that uses bulk_create must validate
        before the call. The state-envelope importer and the flat
        importer both do this explicitly.
        """
        Question.objects.bulk_create([
            Question(
                question='Bulk?',
                choices=['A', 'B'],
                correct_answer=99,
                owned_by=self.user,
                authored_by=self.user,
            ),
        ])
        self.assertEqual(Question.objects.get().correct_answer, 99)

    def test_update_bypasses_the_save_guard(self):
        """
        Same asymmetry for `.update()`. The serializer-driven edit
        path (`QuestionService.update_question`) does its own range
        validation before calling `.update()`.
        """
        q = self._make()
        Question.objects.filter(id=q.id).update(correct_answer=99)
        q.refresh_from_db()
        self.assertEqual(q.correct_answer, 99)