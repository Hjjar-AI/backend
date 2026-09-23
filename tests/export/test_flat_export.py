# tests/export/test_flat_export.py
import json
from pathlib import Path

from apps.questions.models import Question
from apps.questions.services.exporting import ExportService
from apps.questions.services.exporting.formula_sanitizer import (
    sanitize_formula_cell,
)
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question, make_tag


class FlatExportTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('alice')
        make_question(owner=self.author, question='First exported?')
        make_question(
            owner=self.author, question='Second exported?', verified=True,
        )

    def test_csv_export_writes_file(self):
        result = ExportService.export_questions(fmt='csv')
        self.assertIn('filepath', result)
        self.assertIn('filename', result)
        self.assertTrue(Path(result['filepath']).exists())
        content = Path(result['filepath']).read_text(encoding='utf-8-sig')
        self.assertIn('First exported?', content)
        self.assertIn('Second exported?', content)

    def test_verified_only_excludes_unverified(self):
        result = ExportService.export_questions(fmt='csv', verified_only=True)
        content = Path(result['filepath']).read_text(encoding='utf-8-sig')
        self.assertIn('Second exported?', content)
        self.assertNotIn('First exported?', content)

    def test_json_export_includes_choices_list(self):
        result = ExportService.export_questions(fmt='json')
        data = json.loads(
            Path(result['filepath']).read_text(encoding='utf-8')
        )
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        self.assertIsInstance(data[0]['choices'], list)

    def test_json_export_preserves_formula_prefixes_as_data(self):
        question = make_question(
            owner=self.author,
            question='=A legitimate leading equals sign',
            choices=['+Positive', '-Negative'],
            explanation='@Handle',
        )
        result = ExportService.export_questions(fmt='json')
        data = json.loads(Path(result['filepath']).read_text(encoding='utf-8'))
        exported = next(item for item in data if item['uuid'] == str(question.uuid))

        self.assertEqual(exported['question'], question.question)
        self.assertEqual(exported['choices'], question.choices)
        self.assertEqual(exported['explanation'], question.explanation)

    def test_json_keeps_legacy_tags_and_adds_lossless_tag_names(self):
        question = Question.objects.first()
        question.tags.add(make_tag('renal, acute'), make_tag('priority'))

        result = ExportService.export_questions(fmt='json')
        data = json.loads(Path(result['filepath']).read_text(encoding='utf-8'))
        exported = next(item for item in data if item['uuid'] == str(question.uuid))

        self.assertIsInstance(exported['tags'], str)
        self.assertEqual(set(exported['tag_names']), {'renal, acute', 'priority'})

    def test_no_questions_returns_404_error(self):
        Question.objects.all().delete()
        result = ExportService.export_questions(fmt='csv')
        self.assertEqual(result.get('code'), 404)

    def test_unknown_format_returns_400(self):
        result = ExportService.export_questions(fmt='nonsense')
        self.assertEqual(result.get('code'), 400)


class FormulaSanitizerTests(CacheClearingTestCase):
    """
    Sanitizer behavior:

      • `=`, `+`, `-`, `@`, `\\t`, `\\r` at the START of the value
        (after stripping only ASCII spaces) are escaped with a
        leading apostrophe.

      • Leading ASCII spaces are stripped for the CHECK so that
        "  =cmd" is caught. The apostrophe is prepended to the
        ORIGINAL value, so the spaces survive in the output.

      • Leading tabs and CRs are NOT stripped — they are trigger
        characters and must survive to the check so they can be
        escaped. Earlier revisions used `lstrip()` here, which
        stripped them before the check ran and left the value
        un-escaped. That was a real formula-injection hole.
    """

    def test_equals_prefix_is_escaped(self):
        self.assertEqual(sanitize_formula_cell('=cmd'), "'=cmd")

    def test_plus_prefix_is_escaped(self):
        self.assertEqual(sanitize_formula_cell('+cmd'), "'+cmd")

    def test_minus_prefix_is_escaped(self):
        self.assertEqual(sanitize_formula_cell('-cmd'), "'-cmd")

    def test_at_prefix_is_escaped(self):
        self.assertEqual(sanitize_formula_cell('@cmd'), "'@cmd")

    def test_leading_tab_is_escaped(self):
        self.assertEqual(sanitize_formula_cell('\tcmd'), "'\tcmd")

    def test_leading_cr_is_escaped(self):
        self.assertEqual(sanitize_formula_cell('\rcmd'), "'\rcmd")

    def test_leading_space_before_trigger_is_escaped(self):
        # The spaces survive in the output (the apostrophe is
        # prepended to the original) but the trigger char was
        # detected.
        self.assertEqual(sanitize_formula_cell('  =cmd'), "'  =cmd")

    def test_leading_spaces_before_tab_are_escaped(self):
        self.assertEqual(sanitize_formula_cell('  \tcmd'), "'  \tcmd")

    def test_plain_text_unchanged(self):
        self.assertEqual(sanitize_formula_cell('normal'), 'normal')

    def test_empty_and_none_pass_through(self):
        self.assertEqual(sanitize_formula_cell(''), '')
        self.assertIsNone(sanitize_formula_cell(None))

    def test_whitespace_only_is_unchanged(self):
        # No trigger character after the (empty) stripped prefix —
        # the value is returned untouched.
        self.assertEqual(sanitize_formula_cell('   '), '   ')

    def test_non_string_passes_through(self):
        self.assertEqual(sanitize_formula_cell(42), 42)

    def test_exported_question_text_is_sanitized(self):
        user = make_user('bob')
        make_question(owner=user, question='=HYPERLINK("evil")')
        result = ExportService.export_questions(fmt='csv')
        content = Path(result['filepath']).read_text(encoding='utf-8-sig')
        self.assertIn("'=HYPERLINK", content)
