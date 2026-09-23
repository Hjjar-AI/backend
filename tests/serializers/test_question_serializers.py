# tests/serializers/test_question_serializers.py
from django.test import SimpleTestCase

from apps.questions.models import Question
from apps.questions.serializers import (
    QuestionCreateSerializer,
    QuestionUpdateSerializer,
)


class QuestionCreateSerializerTests(SimpleTestCase):
    def _s(self, data):
        return QuestionCreateSerializer(data=data)

    def _valid(self, **kw):
        base = {
            'question': 'Valid question?',
            'choices': ['A', 'B', 'C'],
            'correct_answer': 1,
            'difficulty': 'medium',
        }
        base.update(kw)
        return base

    def test_happy_path(self):
        s = self._s(self._valid())
        self.assertTrue(s.is_valid(), s.errors)

    def test_provenance_and_multilingual_fields_are_normalized(self):
        s = self._s(self._valid(
            source_document='source.pdf',
            source_page=9,
            translations={
                'EN_us': {
                    'question': 'Translated question?',
                    'choices': ['Yes', 'No'],
                    'explanation': 'Explanation',
                },
            },
        ))

        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data['source_document'], 'source.pdf')
        self.assertEqual(s.validated_data['source_page'], 9)
        self.assertIn('en-US', s.validated_data['translations'])

    def test_translation_requires_question_text(self):
        s = self._s(self._valid(translations={
            'ar': {'choices': ['أ', 'ب']},
        }))

        self.assertFalse(s.is_valid())
        self.assertIn('translations', s.errors)

    def test_missing_question_rejected(self):
        data = self._valid()
        del data['question']
        self.assertFalse(self._s(data).is_valid())

    def test_one_choice_rejected(self):
        self.assertFalse(
            self._s(self._valid(choices=['only one'])).is_valid()
        )

    def test_out_of_range_correct_answer_rejected(self):
        self.assertFalse(
            self._s(self._valid(correct_answer=99)).is_valid()
        )

    def test_case_key_whitespace_stripped(self):
        s = self._s(self._valid(case_key='  case-1  '))
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data['case_key'], 'case-1')

    def test_case_key_blank_normalizes_to_none(self):
        s = self._s(self._valid(case_key='   '))
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data['case_key'])

    def test_authored_by_not_writable(self):
        # The serializer's Meta.fields does not include authored_by or
        # owned_by — DRF silently drops unknown keys. A caller cannot
        # smuggle authorship through this serializer.
        data = self._valid(authored_by=999, owned_by=999)
        s = self._s(data)
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn('authored_by', s.validated_data)
        self.assertNotIn('owned_by', s.validated_data)


class QuestionUpdateSerializerTests(SimpleTestCase):
    def _instance(self):
        return Question(
            question='x', choices=['A', 'B'], correct_answer=1,
        )

    def test_partial_update_without_choices(self):
        s = QuestionUpdateSerializer(
            self._instance(), data={'difficulty': 'hard'}, partial=True,
        )
        self.assertTrue(s.is_valid(), s.errors)

    def test_new_choices_requires_valid_range(self):
        s = QuestionUpdateSerializer(
            self._instance(),
            data={'choices': ['A', 'B', 'C'], 'correct_answer': 5},
            partial=True,
        )
        self.assertFalse(s.is_valid())

    def test_new_choices_alone_uses_instance_correct_answer(self):
        # Instance has correct_answer=1 with 2 choices. New choices
        # has 3 entries. The existing 1 is still valid; no error.
        s = QuestionUpdateSerializer(
            self._instance(),
            data={'choices': ['A', 'B', 'C']},
            partial=True,
        )
        self.assertTrue(s.is_valid(), s.errors)

    def test_authored_by_not_writable(self):
        s = QuestionUpdateSerializer(
            self._instance(),
            data={'authored_by': 999, 'question': 'ok?'},
            partial=True,
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn('authored_by', s.validated_data)
