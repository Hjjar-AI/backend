# tests/questions/test_state_roundtrip.py
"""
Export → wipe → import round trip.

This is the one test that catches drift between the exporter and
the importer. Both files must agree on:
  • every field name in the current package schema
  • the shape of every section (categories, tags, cases, questions)
  • the uuid-as-identity contract
  • which fields are portable and which are local-only

A drift anywhere in that list is invisible to the earlier tests
(which exercise export and import in isolation) and only surfaces
in production when someone actually tries to restore a backup.

OWNERSHIP TRANSFER
------------------
The importer OVERRIDES `owned_by` to the acting importer on every
import — this is a design decision documented in
`state_export.py`: ownership is local by definition, whoever
restores the backup is the steward. So a round-trip test that
imports as a DIFFERENT user than the original author will see
`owned_by_uuid` change between the two exports. That is correct
behavior, not a bug.

The byte-identical test below therefore imports as the ORIGINAL
AUTHOR. That is the honest statement of the invariant: "a backup
restored by its own author is byte-identical to the source."
"""
import json
import shutil
import tempfile
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.questions.models import Question, Category, Tag, ClinicalCase
from apps.questions.services import ImportService
from apps.questions.services.exporting import ExportService
from tests.base import CacheClearingTestCase
from tests.factories import (
    make_user, make_question, make_category, make_tag, make_case,
)


class StateRoundTripTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp()
        self.override = override_settings(EXPORT_FOLDER=self.tmpdir)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        super().tearDown()

    def _export(self, fmt='json'):
        return ExportService.export_state(include_images=False, fmt=fmt)

    def _import_file(self, filepath, username='importer'):
        with open(filepath, 'rb') as f:
            raw = f.read()
        is_xlsx = str(filepath).lower().endswith('.xlsx')
        upload = SimpleUploadedFile(
            'state.xlsx' if is_xlsx else 'state.json',
            raw,
            content_type=(
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                if is_xlsx else 'application/json'
            ),
        )
        return ImportService.import_state(upload, username, mode='merge')

    def test_xlsx_roundtrip_uses_the_same_state_import_pipeline(self):
        author = make_user('xlsx_author')
        original = make_question(owner=author, question='Excel roundtrip?')

        result = self._export(fmt='xlsx')
        Question.objects.all().delete()

        import_result = self._import_file(
            result['filepath'], username='xlsx_author',
        )
        self.assertNotIn('error', import_result)
        restored = Question.objects.get()
        self.assertEqual(restored.uuid, original.uuid)
        self.assertEqual(restored.question, 'Excel roundtrip?')

    def test_roundtrip_creates_same_uuids(self):
        author = make_user('author')
        q = make_question(owner=author, question='Roundtrip?')
        original_uuid = str(q.uuid)

        result = self._export()
        Question.objects.all().delete()
        self.assertEqual(Question.objects.count(), 0)
        make_user('importer')

        import_result = self._import_file(result['filepath'])
        self.assertEqual(import_result['counts']['questions_created'], 1)

        restored = Question.objects.get()
        self.assertEqual(str(restored.uuid), original_uuid)
        self.assertEqual(restored.question, 'Roundtrip?')

    def test_roundtrip_preserves_case_linkage(self):
        author = make_user('author')
        case = make_case('roundtrip-case', stem='A vignette')
        make_question(owner=author, case=case, question='In case?')

        result = self._export()
        ClinicalCase.objects.all().delete()
        Question.objects.all().delete()
        make_user('importer')

        self._import_file(result['filepath'])

        restored_q = Question.objects.get()
        self.assertIsNotNone(restored_q.case_id)
        self.assertEqual(restored_q.case.key, 'roundtrip-case')
        self.assertEqual(restored_q.case.stem, 'A vignette')

    def test_roundtrip_preserves_category_and_tags(self):
        author = make_user('author')
        cat = make_category('roundtrip-cat')
        tag_a = make_tag('roundtrip-tag-a')
        tag_b = make_tag('roundtrip-tag-b')
        q = make_question(owner=author, category=cat)
        q.tags.add(tag_a, tag_b)

        result = self._export()
        Tag.objects.all().delete()
        Category.objects.all().delete()
        Question.objects.all().delete()
        make_user('importer')

        self._import_file(result['filepath'])

        restored = Question.objects.get()
        self.assertEqual(restored.category.name, 'roundtrip-cat')
        self.assertEqual(
            set(restored.tags.values_list('name', flat=True)),
            {'roundtrip-tag-a', 'roundtrip-tag-b'},
        )

    def test_roundtrip_preserves_authorship_by_uuid(self):
        author = make_user('author')
        make_question(owner=author)

        result = self._export()
        Question.objects.all().delete()
        make_user('importer')

        self._import_file(result['filepath'])

        restored = Question.objects.get()
        self.assertEqual(restored.authored_by, author)

    def test_roundtrip_preserves_verification_fields(self):
        author = make_user('author')
        make_question(
            owner=author, verified=True,
            verified_by='moderator', verification_notes='looks good',
        )

        result = self._export()
        Question.objects.all().delete()
        make_user('importer')

        self._import_file(result['filepath'])

        restored = Question.objects.get()
        self.assertTrue(restored.verified)
        self.assertEqual(restored.verified_by, 'moderator')
        self.assertEqual(restored.verification_notes, 'looks good')

    def test_roundtrip_preserves_statistics_version_and_audit_fields(self):
        author = make_user('stats_author')
        original = make_question(owner=author)
        original.times_answered = 12
        original.times_correct = 8
        original.version = 5
        original.updated_by = 'auditor'
        original.save(update_fields=[
            'times_answered', 'times_correct', 'version', 'updated_by',
        ])
        original_created_at = original.created_at
        original_updated_at = original.updated_at

        result = self._export()
        Question.objects.all().delete()

        self._import_file(result['filepath'], username='stats_author')

        restored = Question.objects.get()
        self.assertEqual(restored.times_answered, 12)
        self.assertEqual(restored.times_correct, 8)
        self.assertEqual(restored.version, 5)
        self.assertEqual(restored.updated_by, 'auditor')
        self.assertEqual(restored.created_at, original_created_at)
        self.assertEqual(restored.updated_at, original_updated_at)

    def test_importer_becomes_owner_by_design(self):
        """
        The complement of the byte-identical test: importing as a
        DIFFERENT user than the original author MUST transfer
        ownership. This is the documented design decision, and it
        deserves a test of its own so a future refactor cannot
        silently change it back to preserving ownership.
        """
        author = make_user('author')
        make_question(owner=author)

        result = self._export()
        Question.objects.all().delete()
        importer = make_user('importer')

        self._import_file(result['filepath'], username='importer')

        restored = Question.objects.get()
        self.assertEqual(restored.authored_by, author)   # credit preserved
        self.assertEqual(restored.owned_by, importer)    # steward transferred

    def test_second_roundtrip_is_byte_identical(self):
        """
        Export → import (as the original author) → export must
        produce the same envelope modulo the volatile `meta` fields.

        Importing as the original author is required: a restore by
        a different user transfers ownership, and ownership appears
        in the envelope as `owned_by_uuid`. The `meta.exported_at`
        timestamp is not compared — the sections are.
        """
        author = make_user('author')
        q = make_question(owner=author, question='Stable?')
        cat = make_category('stable-cat')
        q.category = cat
        q.save()

        first_export = self._export()
        with open(first_export['filepath']) as f:
            first_payload = json.load(f)

        Question.objects.all().delete()
        Category.objects.all().delete()
        # Reimport as the SAME author. See the module docstring.
        self._import_file(first_export['filepath'], username='author')

        second_export = self._export()
        with open(second_export['filepath']) as f:
            second_payload = json.load(f)

        for key in ('categories', 'tags', 'cases', 'questions'):
            self.assertEqual(
                first_payload[key], second_payload[key],
                f'section {key!r} differs between first and second export',
            )

    def test_drafts_survive_roundtrip_as_drafts(self):
        author = make_user('author')
        make_question(
            owner=author, question='Draft survives?',
            is_draft=True, draft_owner=author,
        )

        result = self._export()
        Question.objects.all().delete()
        importer = make_user('importer')

        self._import_file(result['filepath'], username='importer')

        restored = Question.objects.get()
        self.assertTrue(restored.is_draft)
        # Draft ownership always transfers to the acting importer
        # per the documented policy — the original draft_owner may
        # not exist on the target system.
        self.assertEqual(restored.draft_owner, importer)
