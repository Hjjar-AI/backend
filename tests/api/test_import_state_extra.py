# tests/api/test_import_state_extra.py
"""
Additional ImportStateView coverage. The baseline file exercises
the analyze / dry_run / write dispatch, the admin password gate,
and the mapping JSON parse. This file covers the replace mode's
interaction with master exams, the file-size gate, and the MIME
check's fail-closed behavior.

MIME PATCH STACKING
-------------------
The class-level patcher replaces `entrypoint.verify_upload_mime`
with a no-op. To test the fail-closed path, this file layers a
SECOND patch on top inside the test body rather than stopping the
first. The inner `with` block shadows the outer patcher and, when
it exits, the outer one resumes — no state to unwind.

FILE SIZE AND THE TEST CLIENT
-----------------------------
Setting `upload.size` on a `SimpleUploadedFile` fixture does NOT
change what the server sees. The test client serializes the file
into a multipart body; the server parses that body and rebuilds an
UploadedFile whose `size` is computed from the actual bytes. The
only way to make the server observe an oversized upload is either
to send real bytes or to shrink `MAX_UPLOAD_SIZE` for the test.
The test below uses `override_settings` on the latter — it keeps
the payload tiny and the runtime fast.
"""
import json
import uuid as uuid_mod
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.master_exams.models import MasterExam, MasterExamQuestion
from tests.base import CacheClearingTestCase
from tests.factories import make_admin, make_question


def _envelope(questions=()):
    return {
        'meta': {
            'format': 'mukhtabir-questions',
            'version': 2,
            'exported_at': '2026-01-01T00:00:00',
            'includes_images': False,
            'user_map': {},
            'counts': {},
        },
        'categories': [],
        'tags': [],
        'cases': [],
        'questions': list(questions),
    }


def _upload(payload):
    raw = json.dumps(payload).encode('utf-8')
    return SimpleUploadedFile(
        'state.json', raw, content_type='application/json',
    )


def _question_entry(**overrides):
    entry = {
        'uuid': str(uuid_mod.uuid4()),
        'question': 'Valid question?',
        'choices': ['A', 'B'],
        'correct_answer': 1,
        'explanation': '',
        'source': '',
        'difficulty': 'medium',
        'category_uuid': None,
        'tags': [],
        'case_uuid': None,
        'case_order': None,
        'is_draft': False,
        'verified': False,
        'verified_by': None,
        'verified_at': None,
        'verification_notes': None,
        'authored_by_uuid': None,
        'authored_by_name': None,
        'owned_by_uuid': None,
        'owned_by_name': None,
        'image': None,
    }
    entry.update(overrides)
    return entry


_MIME_PATCH_TARGET = (
    'apps.questions.services.importing.state_import.entrypoint.verify_upload_mime'
)


class ImportStateExtraTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = make_admin('admin_a', 'admin-pw-1234')
        self.client.force_login(self.admin)
        self._mime_patcher = patch(_MIME_PATCH_TARGET, return_value=None)
        self._mime_patcher.start()

    def tearDown(self):
        self._mime_patcher.stop()
        super().tearDown()

    def _post(self, data):
        return self.client.post(
            '/api/v1/database/import/state/', data, format='multipart',
        )

    def test_replace_blocked_by_master_exam_returns_409(self):
        orphan = make_question(owner=self.admin, question='Orphan?')
        now = timezone.now()
        exam = MasterExam.objects.create(
            name='Blocker',
            primary_attending=self.admin,
            opens_at=now + timedelta(hours=1),
            closes_at=now + timedelta(hours=2),
            duration_minutes=60,
        )
        MasterExamQuestion.objects.create(
            master_exam=exam, question=orphan, order=1,
        )

        resp = self._post({
            'file': _upload(_envelope()),
            'mode': 'replace',
            'admin_password': 'admin-pw-1234',
        })
        self.assertEqual(resp.status_code, 409)
        self.assertIn('امتحانات رئيسية', resp.json()['message'])

    def test_replace_succeeds_when_exam_references_covered_question(self):
        covered_uuid = str(uuid_mod.uuid4())
        covered = make_question(
            owner=self.admin, question='Covered?', uuid=covered_uuid,
        )
        now = timezone.now()
        exam = MasterExam.objects.create(
            name='References covered',
            primary_attending=self.admin,
            opens_at=now + timedelta(hours=1),
            closes_at=now + timedelta(hours=2),
            duration_minutes=60,
        )
        MasterExamQuestion.objects.create(
            master_exam=exam, question=covered, order=1,
        )

        entry = {
            'uuid': covered_uuid,
            'question': 'Still covered?',
            'choices': ['A', 'B'],
            'correct_answer': 1,
            'explanation': '',
            'source': '',
            'difficulty': 'medium',
            'category_uuid': None,
            'tags': [],
            'case_uuid': None,
            'case_order': None,
            'is_draft': False,
            'verified': False,
            'verified_by': None,
            'verified_at': None,
            'verification_notes': None,
            'authored_by_uuid': None,
            'authored_by_name': None,
            'owned_by_uuid': None,
            'owned_by_name': None,
            'image': None,
        }

        resp = self._post({
            'file': _upload(_envelope([entry])),
            'mode': 'replace',
            'admin_password': 'admin-pw-1234',
        })
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_wrong_envelope_format_returns_400(self):
        payload = _envelope()
        payload['meta']['format'] = 'something-else'
        resp = self._post({'file': _upload(payload)})
        self.assertEqual(resp.status_code, 400)

    def test_wrong_envelope_version_returns_400(self):
        payload = _envelope()
        payload['meta']['version'] = 99
        resp = self._post({'file': _upload(payload)})
        self.assertEqual(resp.status_code, 400)

    def test_wrong_file_extension_returns_400(self):
        upload = SimpleUploadedFile(
            'state.txt', b'{}', content_type='text/plain',
        )
        resp = self._post({'file': upload})
        self.assertEqual(resp.status_code, 400)

    @override_settings(MAX_UPLOAD_SIZE=100)
    def test_oversized_upload_rejected(self):
        """
        Shrink the size limit for this test rather than sending a
        real 50 MB payload. The server reconstructs the upload from
        the multipart body, so the only `size` the view sees is the
        actual byte count.
        """
        upload = SimpleUploadedFile(
            'state.json', b'x' * 200, content_type='application/json',
        )
        resp = self._post({'file': upload})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('يتجاوز الحد الأقصى', resp.json()['message'])

    def test_mime_check_fail_closed_on_missing_library(self):
        """
        If python-magic is not installed, `verify_upload_mime`
        returns a 500. The inner patch shadows the class-level
        no-op patch without stopping it — the outer one resumes
        cleanly when the `with` block exits.
        """
        with patch(
            _MIME_PATCH_TARGET,
            return_value={'error': 'MIME unavailable', 'code': 500},
        ):
            resp = self._post({'file': _upload(_envelope())})
        self.assertEqual(resp.status_code, 500)

    def test_missing_required_section_returns_400(self):
        payload = _envelope()
        del payload['questions']
        resp = self._post({'file': _upload(payload)})

        self.assertEqual(resp.status_code, 400)
        self.assertIn('questions', resp.json()['message'])

    def test_malformed_question_field_returns_400_not_500(self):
        entry = _question_entry(source=['not', 'text'])
        resp = self._post({'file': _upload(_envelope([entry]))})

        self.assertEqual(resp.status_code, 400)
        self.assertIn('source', resp.json()['message'])

    def test_duplicate_entity_uuid_returns_400(self):
        duplicate = str(uuid_mod.uuid4())
        payload = _envelope([
            _question_entry(uuid=duplicate, question='First?'),
            _question_entry(uuid=duplicate, question='Second?'),
        ])
        resp = self._post({'file': _upload(payload)})

        self.assertEqual(resp.status_code, 400)
        self.assertIn('مكرر', resp.json()['message'])

    def test_missing_reference_returns_400_instead_of_dropping_it(self):
        entry = _question_entry(category_uuid=str(uuid_mod.uuid4()))
        resp = self._post({'file': _upload(_envelope([entry]))})

        self.assertEqual(resp.status_code, 400)
        self.assertIn('غير موجود', resp.json()['message'])
