# tests/questions/test_state_import.py
"""
State envelope import — merge vs replace semantics.

The behavior under test is the fix documented in
state_import/apply.py: replace mode is an in-place upsert keyed on
uuid, so Bookmark / QuestionFlag / QuestionRating / UserQuestionAttempt
rows referencing an updated question survive. Only public orphans
are deleted, and drafts not covered by the envelope survive.
"""
import json
import uuid as uuid_mod
from io import BytesIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.questions.models import Question
from apps.questions.services import ImportService
from apps.feedback.models import Bookmark, QuestionFlag
from apps.users.models import User
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question, make_category


def _envelope(questions, categories=None, tags=None, cases=None):
    return {
        'meta': {
            'format': 'mukhtabir-questions',
            'version': 2,
            'exported_at': '2026-01-01T00:00:00',
            'includes_images': False,
            'user_map': {},
            'counts': {},
        },
        'categories': categories or [],
        'tags': tags or [],
        'cases': cases or [],
        'questions': questions,
    }


def _q_entry(uuid_str, text='Imported?', **kw):
    entry = {
        'uuid': uuid_str,
        'question': text,
        'choices': ['A', 'B', 'C'],
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
    entry.update(kw)
    return entry


def _upload(payload):
    raw = json.dumps(payload).encode('utf-8')
    f = SimpleUploadedFile('state.json', raw, content_type='application/json')
    return f


class MergeModeTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.acting = make_user('acting')
        self.existing_uuid = str(uuid_mod.uuid4())
        self.existing_q = make_question(
            owner=self.acting,
            question='Original?',
            uuid=self.existing_uuid,
        )

    def test_merge_skips_existing_uuid(self):
        payload = _envelope([
            _q_entry(self.existing_uuid, text='Imported replacement?'),
            _q_entry(str(uuid_mod.uuid4()), text='Brand new?'),
        ])
        result = ImportService.import_state(
            _upload(payload), 'acting', mode='merge',
        )
        self.assertEqual(result['counts']['questions_skipped'], 1)
        self.assertEqual(result['counts']['questions_created'], 1)

        self.existing_q.refresh_from_db()
        self.assertEqual(self.existing_q.question, 'Original?')

    def test_merge_does_not_delete_anything(self):
        payload = _envelope([_q_entry(str(uuid_mod.uuid4()))])
        ImportService.import_state(_upload(payload), 'acting', mode='merge')
        self.assertTrue(
            Question.objects.filter(id=self.existing_q.id).exists()
        )

    def test_use_imported_strategy_updates_a_conflicting_question(self):
        payload = _envelope([
            _q_entry(self.existing_uuid, text='Imported wins?'),
        ])
        result = ImportService.import_state(
            _upload(payload), 'acting', mode='merge',
            conflict_strategy='use_imported',
        )

        self.assertEqual(result['counts']['questions_updated'], 1)
        self.existing_q.refresh_from_db()
        self.assertEqual(self.existing_q.question, 'Imported wins?')

    def test_review_strategy_applies_each_question_resolution(self):
        payload = _envelope([
            _q_entry(self.existing_uuid, text='Reviewed import?'),
        ])
        analysis = ImportService.import_state(
            _upload(payload), 'acting', mode='merge',
            analyze=True, conflict_strategy='review',
        )
        self.assertEqual(analysis['conflict_count'], 1)
        self.assertIn('question', analysis['conflicts'][0]['changed_fields'])

        ImportService.import_state(
            _upload(payload), 'acting', mode='merge',
            conflict_strategy='review',
            conflict_resolutions={self.existing_uuid: 'use_imported'},
        )
        self.existing_q.refresh_from_db()
        self.assertEqual(self.existing_q.question, 'Reviewed import?')

    def test_v2_package_is_migrated_before_import(self):
        payload = _envelope([_q_entry(str(uuid_mod.uuid4()), text='Legacy v2?')])
        result = ImportService.import_state(_upload(payload), 'acting', mode='merge')

        self.assertEqual(result['migrations_applied'], ['2→3', '3→4'])
        self.assertTrue(Question.objects.filter(question='Legacy v2?').exists())

    def test_duplicate_question_and_choices_are_marked_and_flagged(self):
        duplicate_uuid = str(uuid_mod.uuid4())
        payload = _envelope([
            _q_entry(
                duplicate_uuid,
                text='Original?',
                choices=['Same', 'Same', 'Different'],
            ),
        ])
        result = ImportService.import_state(_upload(payload), 'acting', mode='merge')

        imported = Question.objects.get(uuid=duplicate_uuid)
        self.assertIn('[DUP-', imported.question)
        self.assertIn('[DUP-', imported.choices[1])
        self.assertEqual(result['counts']['duplicates_marked'], 1)
        self.assertTrue(QuestionFlag.objects.filter(
            question=imported,
            resolved=False,
            reason__startswith='[DATA_QUALITY]',
        ).exists())


class ReplaceModeTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.acting = make_user('acting')
        self.covered_uuid = str(uuid_mod.uuid4())
        self.covered_q = make_question(
            owner=self.acting,
            question='Covered by envelope?',
            uuid=self.covered_uuid,
        )
        self.orphan_q = make_question(
            owner=self.acting,
            question='Orphaned public?',
        )
        self.draft_q = make_question(
            owner=self.acting,
            question='Draft orphan?',
            is_draft=True,
            draft_owner=self.acting,
        )

    def test_replace_updates_in_place_preserving_pk(self):
        old_pk = self.covered_q.pk
        payload = _envelope([
            _q_entry(self.covered_uuid, text='New text?'),
        ])
        result = ImportService.import_state(
            _upload(payload), 'acting', mode='replace',
        )
        self.assertEqual(result['counts']['questions_updated'], 1)
        self.covered_q.refresh_from_db()
        self.assertEqual(self.covered_q.pk, old_pk)
        self.assertEqual(self.covered_q.question, 'New text?')

    def test_replace_from_older_v2_preserves_stats_missing_from_envelope(self):
        self.covered_q.times_answered = 11
        self.covered_q.times_correct = 7
        self.covered_q.version = 4
        self.covered_q.save(update_fields=[
            'times_answered', 'times_correct', 'version',
        ])
        payload = _envelope([_q_entry(self.covered_uuid)])

        ImportService.import_state(_upload(payload), 'acting', mode='replace')

        self.covered_q.refresh_from_db()
        self.assertEqual(self.covered_q.times_answered, 11)
        self.assertEqual(self.covered_q.times_correct, 7)
        self.assertEqual(self.covered_q.version, 4)

    def test_replace_preserves_through_fk_rows(self):
        """
        The whole point of the in-place upsert. A Bookmark row
        pointing at the covered question must survive an import
        that merely updates its text.
        """
        bookmark = Bookmark.objects.create(
            user=self.acting, question=self.covered_q,
        )
        payload = _envelope([
            _q_entry(self.covered_uuid, text='Reimported?'),
        ])
        ImportService.import_state(_upload(payload), 'acting', mode='replace')
        self.assertTrue(Bookmark.objects.filter(pk=bookmark.pk).exists())

    def test_replace_deletes_public_orphans(self):
        payload = _envelope([_q_entry(self.covered_uuid)])
        result = ImportService.import_state(
            _upload(payload), 'acting', mode='replace',
        )
        self.assertFalse(
            Question.objects.filter(id=self.orphan_q.id).exists()
        )
        self.assertGreater(result['counts']['questions_deleted'], 0)

    def test_replace_preserves_draft_orphans(self):
        """
        Drafts are per-author working state. Replacing the public
        bank must not wipe them.
        """
        payload = _envelope([_q_entry(self.covered_uuid)])
        ImportService.import_state(_upload(payload), 'acting', mode='replace')
        self.draft_q.refresh_from_db()
        self.assertTrue(self.draft_q.is_draft)

    def test_replace_blocked_by_master_exam(self):
        from apps.master_exams.models import MasterExam, MasterExamQuestion
        from django.utils import timezone
        from datetime import timedelta

        exam = MasterExam.objects.create(
            name='Blocking exam',
            primary_attending=self.acting,
            opens_at=timezone.now() + timedelta(days=1),
            closes_at=timezone.now() + timedelta(days=2),
            duration_minutes=60,
        )
        MasterExamQuestion.objects.create(
            master_exam=exam, question=self.orphan_q, order=1,
        )

        payload = _envelope([_q_entry(self.covered_uuid)])
        result = ImportService.import_state(
            _upload(payload), 'acting', mode='replace',
        )
        self.assertEqual(result.get('code'), 409)

    def test_replace_rejects_a_selective_package(self):
        payload = _envelope([_q_entry(self.covered_uuid)])
        payload['meta'].update({
            'version': 3,
            'scope': 'selection',
            'selection': {'difficulty': 'hard'},
        })

        result = ImportService.import_state(
            _upload(payload), 'acting', mode='replace',
        )

        self.assertEqual(result.get('code'), 409)
        self.assertTrue(Question.objects.filter(id=self.orphan_q.id).exists())


class AnalyzePassTests(CacheClearingTestCase):
    def test_analyze_reports_unknown_authors_without_writing(self):
        make_user('acting')
        payload = _envelope([
            _q_entry(
                str(uuid_mod.uuid4()),
                authored_by_name='Unknown Author',
                authored_by_uuid=str(uuid_mod.uuid4()),
            ),
        ])
        before = Question.objects.count()
        result = ImportService.import_state(
            _upload(payload), 'acting', analyze=True,
        )
        self.assertEqual(Question.objects.count(), before)
        self.assertEqual(len(result['unknown_authors']), 1)
        self.assertEqual(
            result['unknown_authors'][0]['name'], 'Unknown Author',
        )

    def test_analyze_skips_known_local_authors(self):
        acting = make_user('acting')
        payload = _envelope([
            _q_entry(
                str(uuid_mod.uuid4()),
                authored_by_name='acting',
                authored_by_uuid=str(acting.uuid),
            ),
        ])
        result = ImportService.import_state(
            _upload(payload), 'acting', analyze=True,
        )
        self.assertEqual(result['unknown_authors'], [])

    def test_analyze_reports_case_only_authors_for_mapping(self):
        make_user('acting')
        case = {
            'uuid': str(uuid_mod.uuid4()),
            'key': 'case-only-author',
            'title': '',
            'stem': 'Case stem',
            'authored_by_name': 'External Case Author',
            'authored_by_uuid': str(uuid_mod.uuid4()),
        }
        payload = _envelope([], cases=[case])

        result = ImportService.import_state(
            _upload(payload), 'acting', analyze=True,
        )

        self.assertEqual(len(result['unknown_authors']), 1)
        self.assertEqual(result['unknown_authors'][0]['question_count'], 0)
        self.assertEqual(result['unknown_authors'][0]['case_count'], 1)
