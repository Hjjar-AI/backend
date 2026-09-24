# tests/questions/test_state_export.py
import json
import shutil
import tempfile
from pathlib import Path

from django.test import override_settings

from apps.questions.services.exporting import ExportService
from apps.questions.services.exporting.state_export import (
    STATE_FORMAT, STATE_FORMAT_VERSION,
)
from apps.questions.services.state_workbook import read_state_workbook
from apps.questions.models import KnowledgeObject
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
        self.assertEqual(payload['meta']['scope'], 'full')
        self.assertEqual(payload['meta']['selection'], {})
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

    def test_full_question_statistics_and_audit_fields_are_exported(self):
        author = make_user('stats_author')
        question = make_question(owner=author)
        question.times_answered = 9
        question.times_correct = 6
        question.version = 3
        question.updated_by = 'moderator'
        question.save(update_fields=[
            'times_answered', 'times_correct', 'version', 'updated_by',
        ])

        exported = self._payload(ExportService.export_state())['questions'][0]
        self.assertEqual(exported['times_answered'], 9)
        self.assertEqual(exported['times_correct'], 6)
        self.assertEqual(exported['version'], 3)
        self.assertEqual(exported['updated_by'], 'moderator')
        self.assertIsNotNone(exported['created_at'])
        self.assertIsNotNone(exported['updated_at'])

    def test_provenance_and_translations_are_exported(self):
        author = make_user('translated_author')
        question = make_question(owner=author, question='Original?')
        question.source_document = 'source.pdf'
        question.source_page = 17
        question.translations = {
            'ar': {
                'question': 'الأصل؟',
                'choices': ['نعم', 'لا'],
                'explanation': 'شرح',
            },
        }
        question.save(update_fields=[
            'source_document', 'source_page', 'translations',
        ])

        exported = self._payload(ExportService.export_state())['questions'][0]
        self.assertEqual(exported['source_document'], 'source.pdf')
        self.assertEqual(exported['source_page'], 17)
        self.assertEqual(exported['translations'], question.translations)

    def test_linked_knowledge_object_and_revision_date_are_exported(self):
        author = make_user('knowledge_author')
        category = make_category('knowledge-category')
        tag = make_tag('knowledge-tag')
        knowledge = KnowledgeObject.objects.create(
            title='Knowledge concept',
            learning_objective='Explain the knowledge concept',
            canonical_answer='Canonical answer',
            key_facts=['Fact one'],
            misconceptions=['Misconception one'],
            category=category,
            source_document='reference.pdf',
            source_page=8,
            created_by=author,
        )
        knowledge.tags.add(tag)
        question = make_question(
            owner=author,
            category=category,
            knowledge_object=knowledge,
        )

        payload = self._payload(ExportService.export_state())
        exported_question = payload['questions'][0]
        exported_object = payload['knowledge_objects'][0]

        self.assertEqual(
            exported_question['knowledge_object_uuid'], str(knowledge.uuid),
        )
        self.assertEqual(
            exported_question['last_revised_at'],
            question.last_revised_at.isoformat(),
        )
        self.assertEqual(exported_object['uuid'], str(knowledge.uuid))
        self.assertEqual(exported_object['key_facts'], ['Fact one'])
        self.assertEqual(exported_object['tags'], [str(tag.uuid)])

    def test_filters_create_a_selective_portable_package(self):
        author = make_user('selection_author')
        selected_category = make_category('selected')
        other_category = make_category('other')
        make_question(
            owner=author, question='Selected?',
            category=selected_category, difficulty='hard',
        )
        make_question(
            owner=author, question='Excluded?',
            category=other_category, difficulty='easy',
        )

        payload = self._payload(ExportService.export_state(filters={
            'difficulty': 'hard',
            'category_ids': str(selected_category.id),
        }))

        self.assertEqual(payload['meta']['scope'], 'selection')
        self.assertEqual(payload['meta']['selection']['difficulty'], 'hard')
        self.assertEqual(
            [question['question'] for question in payload['questions']],
            ['Selected?'],
        )
        self.assertEqual(
            [category['name'] for category in payload['categories']],
            ['selected'],
        )

    def test_xlsx_is_a_lossless_container_for_state_envelope(self):
        author = make_user('xlsx_author')
        make_question(owner=author, question='Workbook question?')

        result = ExportService.export_state(fmt='xlsx')
        self.assertTrue(result['filename'].endswith('.xlsx'))
        payload = read_state_workbook(result['filepath'])
        self.assertEqual(payload['meta']['format'], STATE_FORMAT)
        self.assertEqual(payload['meta']['version'], STATE_FORMAT_VERSION)
        self.assertEqual(payload['questions'][0]['question'], 'Workbook question?')

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

    def test_tag_ancestor_chain_is_exported_even_when_only_child_is_assigned(self):
        author = make_user('tag_author')
        parent = make_tag('parent-tag')
        child = make_tag('child-tag')
        child.parent = parent
        child.save(update_fields=['parent'])
        question = make_question(owner=author)
        question.tags.add(child)

        payload = self._payload(ExportService.export_state())
        tags = {item['name']: item for item in payload['tags']}
        self.assertEqual(set(tags), {'parent-tag', 'child-tag'})
        self.assertEqual(tags['child-tag']['parent_uuid'], str(parent.uuid))

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

    def test_exported_envelope_is_valid_current_schema(self):
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

    def test_questions_have_all_v3_fields(self):
        author = make_user('author')
        make_question(owner=author)

        payload = self._payload(ExportService.export_state())
        q = payload['questions'][0]
        required = {
            'uuid', 'question', 'choices', 'correct_answer', 'explanation',
            'source', 'source_document', 'source_page', 'translations',
            'difficulty', 'category_uuid', 'tags', 'case_uuid',
            'case_order', 'is_draft', 'verified', 'verified_by',
            'verified_at', 'verification_notes',
            'authored_by_uuid', 'authored_by_name',
            'owned_by_uuid', 'owned_by_name', 'updated_by',
            'times_answered', 'times_correct', 'version',
            'created_at', 'updated_at', 'image',
        }
        missing = required - set(q.keys())
        self.assertEqual(missing, set(), f'missing keys: {missing}')

    @override_settings(MAX_STATE_IMPORT_QUESTIONS=0)
    def test_export_rejects_a_question_count_the_importer_cannot_accept(self):
        make_question(owner=make_user('limit_owner'))
        result = ExportService.export_state()

        self.assertEqual(result.get('code'), 413)
        self.assertIn('يتجاوز الحد', result['error'])

    @override_settings(MAX_STATE_TRANSFER_SIZE=100)
    def test_export_removes_artifact_larger_than_import_limit(self):
        result = ExportService.export_state()

        self.assertEqual(result.get('code'), 413)
        self.assertEqual(
            list(Path(self.tmpdir).glob('question_bank_package*.json')),
            [],
        )
