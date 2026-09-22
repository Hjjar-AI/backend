# tests/questions/test_validation.py
from django.test import SimpleTestCase

from apps.questions.validation import clean_and_validate_choices


class CleanAndValidateChoicesTests(SimpleTestCase):
    def test_happy_path_strips_whitespace(self):
        cleaned, err = clean_and_validate_choices(
            ['  A  ', 'B', ' C '], correct_answer=2,
        )
        self.assertIsNone(err)
        self.assertEqual(cleaned, ['A', 'B', 'C'])

    def test_drops_blank_choices(self):
        cleaned, err = clean_and_validate_choices(
            ['A', '', '  ', 'B'], correct_answer=1,
        )
        self.assertIsNone(err)
        self.assertEqual(cleaned, ['A', 'B'])

    def test_fewer_than_two_choices_is_rejected(self):
        cleaned, err = clean_and_validate_choices(['A'], correct_answer=1)
        self.assertIsNone(cleaned)
        self.assertIsNotNone(err)
        self.assertIsNone(err['field'])

    def test_too_many_choices_is_rejected(self):
        """
        The error message reports the MAXIMUM (8), not the count of
        submitted choices. The assertion looks for the configured
        limit, not the offending count.
        """
        cleaned, err = clean_and_validate_choices(
            ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i'],
            correct_answer=1,
        )
        self.assertIsNone(cleaned)
        self.assertIsNotNone(err)
        # Message reads "... عدد الخيارات 8 على الأكثر"
        self.assertIn('8', err['message'])
        self.assertIn('الأكثر', err['message'])

    def test_oversized_choice_is_rejected(self):
        cleaned, err = clean_and_validate_choices(
            ['A' * 500, 'B'], correct_answer=1,
        )
        self.assertIsNone(cleaned)
        self.assertIsNone(err['field'])
        self.assertIn('300', err['message'])

    def test_case_insensitive_duplicates_are_rejected(self):
        cleaned, err = clean_and_validate_choices(
            ['yes', 'YES'], correct_answer=1,
        )
        self.assertIsNone(cleaned)
        self.assertIn('مكرر', err['message'])

    def test_correct_answer_below_range_is_rejected(self):
        cleaned, err = clean_and_validate_choices(
            ['A', 'B'], correct_answer=0,
        )
        self.assertIsNone(cleaned)
        self.assertEqual(err['field'], 'correct_answer')

    def test_correct_answer_above_range_is_rejected(self):
        cleaned, err = clean_and_validate_choices(
            ['A', 'B'], correct_answer=3,
        )
        self.assertIsNone(cleaned)
        self.assertEqual(err['field'], 'correct_answer')

    def test_correct_answer_none_skips_range_check(self):
        """
        The update serializer relies on this: a partial update that
        touches only, say, `difficulty` must not require the caller to
        resend a valid correct_answer.
        """
        cleaned, err = clean_and_validate_choices(['A', 'B'], correct_answer=None)
        self.assertIsNone(err)
        self.assertEqual(cleaned, ['A', 'B'])

    def test_custom_max_choices_is_honoured(self):
        cleaned, err = clean_and_validate_choices(
            ['A', 'B', 'C'], correct_answer=1, max_choices=2,
        )
        self.assertIsNone(cleaned)
        self.assertIsNotNone(err)
        self.assertIn('2', err['message'])