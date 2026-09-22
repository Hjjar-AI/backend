# tests/questions/test_state_export.py
import json
import shutil
import tempfile

from django.test import override_settings

from apps.questions.services.exporting import ExportService
from apps.questions.services.exporting.state_export import (
    STATE_FORMAT, STATE_FORMAT_VERSION,
)
from tests.base import CacheClearingTestCase
from tests.factories import (
    make_user, make_question, make_category, make_tag, make_case,
)


class StateExportTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp()
        self.override = override_settings(EXPORT_FOLDER=self.tmpdir)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        super().tearDown()

    def _payload(self, result):
        with open(result['filepath'], 'r', encoding='utf-8') as f:
            return json.load(f)

    def test_empty_bank_still_writes_envelope(self):
        result = ExportService.export_state()
        payload = self._payload(result)
        self.assertEqual(payload['meta']['format'], STATE_FORMAT)
        self.assertEqual(payload['meta']['version'], STATE_FORMAT_VERSION)
        self.assertEqual(payload['questions'], [])
        self.assertEqual(payload['categories'], [])
        self.assertEqual(payload['tags'], [])
        self.assertEqual(payload['cases'], [])

    def test_question_uuid_and_authorship_fields(self):
        author = make_user('author')
        make_question(owner=author, question='Only question?')

        payload = self._payload(ExportService.export_state())
        q = payload['questions'][0]
        self.assertIn('uuid', q)
        self.assertEqual(q['authored_by_uuid'], str(author.uuid))
        self.assertEqual(q['authored_by_name'], 'author')
        self.assertEqual(q['owned_by_uuid'], str(author.uuid))

    def test_user_map_covers_referenced_users(self):
        author = make_user('author')
        make_question(owner=author)

        payload = self._payload(ExportService.export_state())
        self.assertIn(str(author.uuid), payload['meta']['user_map'])
        self.assertEqual(
            payload['meta']['user_map'][str(author.uuid)]['username'],
            'author',
        )

    def test_case_authors_are_added_to_user_map(self):
        """
        A case whose author does not currently author or own any
        question must still appear in `user_map`. Otherwise the
        mapping UI has nothing to label.
        """
        case_author = make_user('case_author')
        question_author = make_user('q_author')
        case = make_case('my-case', authored_by=case_author)
        make_question(owner=question_author, case=case)

        payload = self._payload(ExportService.export_state())
        self.assertIn(str(case_author.uuid), payload['meta']['user_map'])

    def test_categories_and_tags_exported_with_uuid(self):
        cat = make_category('cat-a')
        tag = make_tag('tag-a')
        # Username must be >= 3 chars, letters/digits/underscore only.
        q = make_question(owner=make_user('alice'), category=cat)
        q.tags.add(tag)

        payload = self._payload(ExportService.export_state())
        self.assertEqual(len(payload['categories']), 1)
        self.assertEqual(payload['categories'][0]['uuid'], str(cat.uuid))
        self.assertEqual(len(payload['tags']), 1)
        self.assertEqual(payload['tags'][0]['uuid'], str(tag.uuid))

    def test_orphan_categories_not_exported(self):
        """A category with no exported question is dropped."""
        make_category('orphan')
        make_user('alice')

        payload = self._payload(ExportService.export_state())
        names = [c['name'] for c in payload['categories']]
        self.assertNotIn('orphan', names)

    def test_drafts_are_included_by_default(self):
        author = make_user('author')
        make_question(
            owner=author, question='Draft?',
            is_draft=True, draft_owner=author,
        )

        payload = self._payload(ExportService.export_state())
        self.assertEqual(len(payload['questions']), 1)
        self.assertTrue(payload['questions'][0]['is_draft'])

    def test_verified_only_excludes_unverified(self):
        author = make_user('author')
        make_question(owner=author, question='Unverified?')
        make_question(owner=author, question='Verified?', verified=True)

        payload = self._payload(
            ExportService.export_state(verified_only=True)
        )
        texts = [q['question'] for q in payload['questions']]
        self.assertEqual(texts, ['Verified?'])

    def test_include_images_false_drops_image_field(self):
        author = make_user('author')
        make_question(owner=author)

        payload = self._payload(
            ExportService.export_state(include_images=False)
        )
        self.assertIsNone(payload['questions'][0]['image'])

    def test_counts_match_actual_lengths(self):
        author = make_user('author')
        make_category('cat-a')
        make_tag('tag-a')
        make_case('case-a')

        cat = make_category('cat-b')
        tag = make_tag('tag-b')
        case = make_case('case-b')
        q = make_question(owner=author, category=cat, case=case)
        q.tags.add(tag)

        payload = self._payload(ExportService.export_state())
        counts = payload['meta']['counts']
        self.assertEqual(counts['questions'], len(payload['questions']))
        self.assertEqual(counts['categories'], len(payload['categories']))
        self.assertEqual(counts['tags'], len(payload['tags']))
        self.assertEqual(counts['cases'], len(payload['cases']))
        self.assertEqual(counts['users'], len(payload['meta']['user_map']))

    def test_exported_envelope_is_valid_v2(self):
        """
        The envelope this export produces must pass the importer's
        own validation. This is the export-side half of the
        round-trip guarantee.
        """
        from apps.questions.services.importing.state_import.validation import (
            _validate_state_envelope,
        )
        author = make_user('author')
        make_question(owner=author)

        payload = self._payload(ExportService.export_state())
        err = _validate_state_envelope(payload)
        self.assertIsNone(err)

    def test_questions_have_all_v2_fields(self):
        author = make_user('author')
        make_question(owner=author)

        payload = self._payload(ExportService.export_state())
        q = payload['questions'][0]
        required = {
            'uuid', 'question', 'choices', 'correct_answer', 'explanation',
            'source', 'difficulty', 'category_uuid', 'tags', 'case_uuid',
            'case_order', 'is_draft', 'verified', 'verified_by',
            'verified_at', 'verification_notes',
            'authored_by_uuid', 'authored_by_name',
            'owned_by_uuid', 'owned_by_name', 'image',
        }
        missing = required - set(q.keys())
        self.assertEqual(missing, set(), f'missing keys: {missing}')