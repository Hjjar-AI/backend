# tests/questions/test_flat_import.py
"""
ImportService.import_file — the flat XLSX / CSV / JSON path.

Every test patches `verify_upload_mime` so the suite does not
depend on python-magic being installed on the host. The MIME
check itself is a boundary check, not a domain rule; the domain
rules are what these tests exercise.
"""
import csv
import io
import json
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.questions.models import Question
from apps.questions.services import ImportService, ExportService
from tests.base import CacheClearingTestCase
from tests.factories import (
    make_user, make_category, make_tag, make_case, make_question,
)


_MIME_PATCH_TARGET = (
    'apps.questions.services.importing.flat_import.verify_upload_mime'
)


def _csv_upload(rows):
    """Build a SimpleUploadedFile from a list of dicts."""
    if not rows:
        raise ValueError('need at least one row')
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    raw = buffer.getvalue().encode('utf-8')
    return SimpleUploadedFile('questions.csv', raw, content_type='text/csv')


def _json_upload(payload):
    raw = json.dumps(payload).encode('utf-8')
    return SimpleUploadedFile(
        'questions.json', raw, content_type='application/json',
    )


class FlatImportCSVTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('alice')
        self._mime_patcher = patch(_MIME_PATCH_TARGET, return_value=None)
        self._mime_patcher.start()

    def tearDown(self):
        self._mime_patcher.stop()
        super().tearDown()

    def _row(self, **kw):
        base = {
            'question': 'What is 2+2?',
            'choice_1': 'Three',
            'choice_2': 'Four',
            'choice_3': 'Five',
            'correct_answer': 2,
        }
        base.update(kw)
        return base

    def test_valid_csv_creates_question(self):
        result = ImportService.import_file(_csv_upload([self._row()]), 'alice')
        self.assertIn('message', result)
        self.assertEqual(Question.objects.count(), 1)
        q = Question.objects.get()
        self.assertEqual(q.question, 'What is 2+2?')
        self.assertEqual(q.choices, ['Three', 'Four', 'Five'])
        self.assertEqual(q.correct_answer, 2)

    def test_imported_question_owned_and_authored_by_user(self):
        ImportService.import_file(_csv_upload([self._row()]), 'alice')
        q = Question.objects.get()
        self.assertEqual(q.authored_by, self.user)
        self.assertEqual(q.owned_by, self.user)

    def test_imported_question_not_verified(self):
        ImportService.import_file(_csv_upload([self._row()]), 'alice')
        self.assertFalse(Question.objects.get().verified)

    def test_tags_split_on_comma(self):
        ImportService.import_file(
            _csv_upload([self._row(tags='math, arithmetic')]),
            'alice',
        )
        q = Question.objects.get()
        names = set(q.tags.values_list('name', flat=True))
        self.assertEqual(names, {'math', 'arithmetic'})

    def test_difficulty_normalised(self):
        ImportService.import_file(
            _csv_upload([self._row(difficulty='HARD')]), 'alice',
        )
        self.assertEqual(Question.objects.get().difficulty, 'hard')

    def test_invalid_difficulty_falls_back_to_medium(self):
        ImportService.import_file(
            _csv_upload([self._row(difficulty='impossible')]), 'alice',
        )
        self.assertEqual(Question.objects.get().difficulty, 'medium')

    def test_category_by_name(self):
        make_category('Cardiology')
        ImportService.import_file(
            _csv_upload([self._row(category='Cardiology')]), 'alice',
        )
        q = Question.objects.get()
        self.assertEqual(q.category.name, 'Cardiology')

    def test_unknown_category_left_null(self):
        ImportService.import_file(
            _csv_upload([self._row(category='Does not exist')]), 'alice',
        )
        self.assertIsNone(Question.objects.get().category)

    def test_case_group_creates_clinical_case(self):
        ImportService.import_file(
            _csv_upload([self._row(case_group='my-case')]), 'alice',
        )
        q = Question.objects.get()
        self.assertIsNotNone(q.case_id)
        self.assertEqual(q.case.key, 'my-case')
        self.assertEqual(q.case.authored_by, self.user)

    def test_blank_question_row_rejected(self):
        result = ImportService.import_file(
            _csv_upload([self._row(question='   ')]), 'alice',
        )
        self.assertEqual(result.get('code'), 400)

    def test_single_choice_row_rejected(self):
        row = {
            'question': 'Only one choice?',
            'choice_1': 'A',
            'choice_2': '',
            'correct_answer': 1,
        }
        result = ImportService.import_file(_csv_upload([row]), 'alice')
        self.assertEqual(result.get('code'), 400)
        self.assertEqual(Question.objects.count(), 0)

    def test_correct_answer_out_of_range_rejected(self):
        result = ImportService.import_file(
            _csv_upload([self._row(correct_answer=99)]), 'alice',
        )
        self.assertEqual(result.get('code'), 400)

    def test_fractional_correct_answer_is_rejected_not_truncated(self):
        result = ImportService.import_file(
            _csv_upload([self._row(correct_answer='1.5')]), 'alice',
        )
        self.assertEqual(result.get('code'), 400)
        self.assertEqual(Question.objects.count(), 0)

    def test_duplicate_choices_rejected(self):
        row = {
            'question': 'Duplicate?',
            'choice_1': 'Yes',
            'choice_2': 'YES',
            'correct_answer': 1,
        }
        result = ImportService.import_file(_csv_upload([row]), 'alice')
        self.assertEqual(result.get('code'), 400)

    def test_transaction_is_atomic(self):
        """
        A batch with a valid row followed by an invalid row must
        import neither. The `_persist_records` helper wraps the
        whole batch in one transaction.
        """
        rows = [
            self._row(question='Valid?'),
            self._row(question='Invalid?', correct_answer=999),
        ]
        result = ImportService.import_file(_csv_upload(rows), 'alice')
        self.assertEqual(result.get('code'), 400)
        self.assertEqual(Question.objects.count(), 0)

    def test_unknown_extension_rejected(self):
        upload = SimpleUploadedFile(
            'file.txt', b'data', content_type='text/plain',
        )
        result = ImportService.import_file(upload, 'alice')
        self.assertEqual(result.get('code'), 400)

    def test_unknown_user_rejected(self):
        result = ImportService.import_file(_csv_upload([self._row()]), 'ghost')
        self.assertEqual(result.get('code'), 404)

    def test_tags_json_preserves_names_containing_commas(self):
        result = ImportService.import_file(
            _csv_upload([self._row(
                tags='renal, acute,priority',
                tags_json=json.dumps(['renal, acute', 'priority']),
            )]),
            'alice',
        )
        self.assertNotIn('error', result)
        names = set(Question.objects.get().tags.values_list('name', flat=True))
        self.assertEqual(names, {'renal, acute', 'priority'})

    def test_decimal_category_id_is_not_truncated_to_an_integer(self):
        make_category('Should not match', id=1)
        result = ImportService.import_file(
            _csv_upload([self._row(category_id='1.5')]), 'alice',
        )
        self.assertNotIn('error', result)
        self.assertIsNone(Question.objects.get().category)

    @override_settings(MAX_IMPORT_QUESTIONS=1)
    def test_row_limit_rejects_whole_file_instead_of_silent_truncation(self):
        result = ImportService.import_file(
            _csv_upload([
                self._row(question='First?'),
                self._row(question='Second?'),
            ]),
            'alice',
        )
        self.assertEqual(result.get('code'), 400)
        self.assertEqual(Question.objects.count(), 0)


class FlatImportJSONTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        make_user('alice')
        self._mime_patcher = patch(_MIME_PATCH_TARGET, return_value=None)
        self._mime_patcher.start()

    def tearDown(self):
        self._mime_patcher.stop()
        super().tearDown()

    def _entry(self, **kw):
        base = {
            'question': 'JSON import?',
            'choices': ['A', 'B', 'C'],
            'correct_answer': 1,
        }
        base.update(kw)
        return base

    def test_valid_json_array(self):
        result = ImportService.import_file(
            _json_upload([self._entry()]), 'alice',
        )
        self.assertIn('message', result)
        self.assertEqual(Question.objects.count(), 1)

    def test_missing_choices_column_rejected(self):
        result = ImportService.import_file(
            _json_upload([{'question': 'No choices?', 'correct_answer': 1}]),
            'alice',
        )
        self.assertEqual(result.get('code'), 400)

    def test_non_object_json_is_rejected_as_a_client_error(self):
        result = ImportService.import_file(_json_upload('not an object'), 'alice')
        self.assertEqual(result.get('code'), 400)

    def test_non_text_choices_are_rejected_as_a_client_error(self):
        result = ImportService.import_file(
            _json_upload([self._entry(choices=[1, 2])]), 'alice',
        )
        self.assertEqual(result.get('code'), 400)
        self.assertEqual(Question.objects.count(), 0)

    def test_oversized_array_rejected(self):
        # MAX_JSON_IMPORT_ELEMENTS is 10,000.
        payload = [self._entry() for _ in range(10_001)]
        result = ImportService.import_file(_json_upload(payload), 'alice')
        self.assertEqual(result.get('code'), 400)
        self.assertIn('الحد الأقصى', result['error'])

    def test_nested_case_and_case_order_are_imported(self):
        case_uuid = '596533ed-1c1b-4c43-aef1-b831b0a22d69'
        result = ImportService.import_file(
            _json_upload([self._entry(
                case={
                    'uuid': case_uuid,
                    'key': 'portable-case',
                    'title': 'Portable title',
                    'stem': 'Portable stem',
                },
                case_order=3,
            )]),
            'alice',
        )
        self.assertNotIn('error', result)
        question = Question.objects.select_related('case').get()
        self.assertEqual(str(question.case.uuid), case_uuid)
        self.assertEqual(question.case.key, 'portable-case')
        self.assertEqual(question.case.title, 'Portable title')
        self.assertEqual(question.case_order, 3)

    def test_uuid_import_is_idempotent(self):
        entry = self._entry(uuid='498b88e6-7bdd-4d96-b95f-782906f52f0d')
        first = ImportService.import_file(_json_upload([entry]), 'alice')
        second = ImportService.import_file(_json_upload([entry]), 'alice')

        self.assertEqual(first['imported'], 1)
        self.assertEqual(second['imported'], 0)
        self.assertEqual(second['skipped'], 1)
        self.assertEqual(Question.objects.count(), 1)

    def test_json_export_can_round_trip_portable_fields(self):
        owner = make_user('roundtrip_owner')
        category = make_category('Portable category')
        case = make_case(
            'portable-case', title='Case title', stem='Case stem',
            authored_by=owner,
        )
        tag = make_tag('renal, acute')
        original = make_question(
            owner=owner,
            uuid='b14d327d-6958-4dd8-b0db-60a2e6c1e576',
            question='=Preserve this text',
            choices=['+First', '-Second'],
            correct_answer=2,
            category=category,
            case=case,
            case_order=4,
        )
        original.tags.add(tag)
        export = ExportService.export_questions(fmt='json')
        raw = Path(export['filepath']).read_bytes()

        category_uuid = str(category.uuid)
        case_uuid = str(case.uuid)

        original.delete()
        category.delete()
        case.delete()
        tag.delete()
        result = ImportService.import_file(
            SimpleUploadedFile(
                'roundtrip.json', raw, content_type='application/json',
            ),
            'alice',
        )

        self.assertNotIn('error', result)
        imported = Question.objects.select_related('category', 'case').get()
        self.assertEqual(str(imported.uuid), 'b14d327d-6958-4dd8-b0db-60a2e6c1e576')
        self.assertEqual(imported.question, '=Preserve this text')
        self.assertEqual(imported.choices, ['+First', '-Second'])
        self.assertEqual(str(imported.category.uuid), category_uuid)
        self.assertEqual(imported.category.name, 'Portable category')
        self.assertEqual(str(imported.case.uuid), case_uuid)
        self.assertEqual(imported.case.key, 'portable-case')
        self.assertEqual(imported.case_order, 4)
        self.assertEqual(
            list(imported.tags.values_list('name', flat=True)),
            ['renal, acute'],
        )
