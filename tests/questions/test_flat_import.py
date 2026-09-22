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
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.questions.models import Question, Tag, ClinicalCase
from apps.questions.services import ImportService
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_category


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

    def test_oversized_array_rejected(self):
        # MAX_JSON_IMPORT_ELEMENTS is 10,000.
        payload = [self._entry() for _ in range(10_001)]
        result = ImportService.import_file(_json_upload(payload), 'alice')
        self.assertEqual(result.get('code'), 400)
        self.assertIn('الحد الأقصى', result['error'])