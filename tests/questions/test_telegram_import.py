# tests/questions/test_telegram_import.py
"""
ImportService.import_telegram — the Telegram poll-export path.

The exporter emits `messages[i].poll.question` and
`messages[i].poll.answers[j]` with `text`, `chosen`, and `voters`.
Correct answers come from `chosen` when present; otherwise the
answer with the most voters is used and a localized warning is
written into the explanation.

REGRESSION GUARD
----------------
An earlier revision of this file contained a chained assignment:

    result = ImportService.import_file = ImportService.import_telegram(...)

The RHS evaluates to a dict, so the line rebound
`ImportService.import_file` — a static method on the façade class
— to that dict. Every later test in the same process that called
`ImportService.import_file(...)` then failed with
`TypeError: 'dict' object is not callable`. The test that carried
the bug still passed, because it only inspected `result`.

`test_import_file_method_is_intact` below is the guard: it asserts
the method is still callable, so a future re-introduction of that
line fails loudly at the point of the mistake.
"""
import json
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.questions.models import Question
from apps.questions.services import ImportService
from tests.base import CacheClearingTestCase
from tests.factories import make_user


_MIME_PATCH_TARGET = (
    'apps.questions.services.importing.telegram_import.verify_upload_mime'
)


def _upload(payload):
    raw = json.dumps(payload).encode('utf-8')
    return SimpleUploadedFile(
        'telegram.json', raw, content_type='application/json',
    )


def _poll(question, answers, **kw):
    base = {
        'poll': {
            'question': question,
            'answers': [
                {'text': t, 'chosen': c, 'voters': v}
                for (t, c, v) in answers
            ],
        },
    }
    base.update(kw)
    return base


class TelegramImportTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        make_user('alice')
        self._mime_patcher = patch(_MIME_PATCH_TARGET, return_value=None)
        self._mime_patcher.start()

    def tearDown(self):
        self._mime_patcher.stop()
        super().tearDown()

    def test_import_file_method_is_intact(self):
        """
        Regression guard against the chained-assignment bug that
        used to live in test_valid_poll_creates_question. The
        façade's `import_file` must remain the flat-importer
        callable; a rebind to anything else (a dict, a mocked
        return value, an unrelated callable) fails here.
        """
        self.assertTrue(
            callable(ImportService.import_file),
            'ImportService.import_file was rebound away from the '
            'flat-importer method. Check for a chained assignment '
            'like `x = ImportService.import_file = ...` somewhere '
            'in the test suite.',
        )

    def test_valid_poll_creates_question(self):
        payload = {
            'messages': [
                _poll('Which is correct?', [
                    ('A', True, 10),
                    ('B', False, 5),
                    ('C', False, 2),
                ]),
            ],
        }
        result = ImportService.import_telegram(_upload(payload), 'alice')
        self.assertIn('message', result)
        self.assertEqual(Question.objects.count(), 1)
        q = Question.objects.get()
        self.assertEqual(q.question, 'Which is correct?')
        self.assertEqual(q.choices, ['A', 'B', 'C'])
        self.assertEqual(q.correct_answer, 1)

    def test_chosen_flag_decides_correct_answer(self):
        payload = {
            'messages': [
                _poll('Pick the third', [
                    ('A', False, 10),
                    ('B', False, 8),
                    ('C', True, 1),
                ]),
            ],
        }
        ImportService.import_telegram(_upload(payload), 'alice')
        self.assertEqual(Question.objects.get().correct_answer, 3)

    def test_voters_guess_when_no_chosen(self):
        payload = {
            'messages': [
                _poll('No chosen flag', [
                    ('A', False, 2),
                    ('B', False, 15),
                    ('C', False, 7),
                ]),
            ],
        }
        ImportService.import_telegram(_upload(payload), 'alice')
        q = Question.objects.get()
        self.assertEqual(q.correct_answer, 2)
        self.assertIn('تخمين', q.explanation)

    def test_chosen_flag_suppresses_guessed_warning(self):
        payload = {
            'messages': [
                _poll('Has a chosen', [
                    ('A', True, 1),
                    ('B', False, 99),
                ]),
            ],
        }
        ImportService.import_telegram(_upload(payload), 'alice')
        q = Question.objects.get()
        self.assertNotIn('تخمين', q.explanation)

    def test_single_answer_poll_is_skipped(self):
        payload = {
            'messages': [
                _poll('Only one answer', [('A', True, 5)]),
            ],
        }
        result = ImportService.import_telegram(_upload(payload), 'alice')
        self.assertEqual(result.get('code'), 400)
        self.assertEqual(Question.objects.count(), 0)

    def test_empty_poll_question_skipped(self):
        payload = {
            'messages': [
                _poll('', [('A', True, 1), ('B', False, 2)]),
            ],
        }
        result = ImportService.import_telegram(_upload(payload), 'alice')
        self.assertEqual(result.get('code'), 400)

    def test_non_poll_message_skipped(self):
        payload = {
            'messages': [
                {'text': 'just a message, no poll'},
                _poll('Real poll', [('A', True, 1), ('B', False, 2)]),
            ],
        }
        ImportService.import_telegram(_upload(payload), 'alice')
        self.assertEqual(Question.objects.count(), 1)
        self.assertEqual(Question.objects.get().question, 'Real poll')

    def test_missing_messages_list_rejected(self):
        result = ImportService.import_telegram(_upload({}), 'alice')
        self.assertEqual(result.get('code'), 400)

    def test_non_json_file_rejected(self):
        bad = SimpleUploadedFile(
            'nope.txt', b'not-json', content_type='text/plain',
        )
        result = ImportService.import_telegram(bad, 'alice')
        self.assertEqual(result.get('code'), 400)

    def test_unknown_user_rejected(self):
        payload = {
            'messages': [
                _poll('Any?', [('A', True, 1), ('B', False, 2)]),
            ],
        }
        result = ImportService.import_telegram(_upload(payload), 'ghost')
        self.assertEqual(result.get('code'), 404)