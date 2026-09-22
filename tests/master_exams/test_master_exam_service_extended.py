# tests/master_exams/test_master_exam_service_extended.py
"""
Service-layer coverage for the parts of MasterExamService that the
HTTP tests reach indirectly or not at all:
  • update — the optimistic-lock version CAS
  • create — audience_group_ids / audience_user_ids / co_attending_ids
  • add_questions — the visibility gate on another user's drafts
  • reorder_questions — the mismatch and non-numeric paths
  • add_draft — the case_key resolution

EMPTY-UPDATE SEMANTICS
----------------------
`MasterExamService.update` has two CAS branches. When the caller
sends content fields, it does `version = F('version') + 1` and
checks the rowcount. When the caller sends nothing, it does
`version = F('version')` — a no-op write used only to obtain a
rowcount for the stale-version check. The version does NOT advance
in the empty case.

The consequence, from the caller's point of view:

  • An empty update from a stale caller raises
    MODIFIED_BY_ANOTHER_USER — the CAS still fires.
  • An empty update from a fresh caller succeeds and leaves the
    version unchanged — two callers who both observe version N
    and both send an empty update both succeed, because neither
    changed anything.

Both behaviors are pinned below.
"""
from datetime import timedelta

from django.test import RequestFactory
from django.utils import timezone

from apps.master_exams.models import (
    MasterExam, MasterExamQuestion,
)
from apps.master_exams.services import MasterExamService
from tests.base import CacheClearingTestCase
from tests.factories import (
    make_user, make_question, make_case,
)


def _req(user):
    req = RequestFactory().post('/')
    req.user = user
    return req


def _draft_exam(author, questions=(), **kw):
    now = timezone.now()
    defaults = {
        'name': 'Service Exam',
        'primary_attending': author,
        'opens_at': now + timedelta(hours=1),
        'closes_at': now + timedelta(hours=2),
        'duration_minutes': 60,
    }
    defaults.update(kw)
    exam = MasterExam.objects.create(**defaults)
    for i, q in enumerate(questions, 1):
        MasterExamQuestion.objects.create(
            master_exam=exam, question=q, order=i,
        )
    return exam


class UpdateOptimisticLockTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.q = make_question(owner=self.author)
        self.exam = _draft_exam(self.author, [self.q])

    def test_matching_version_updates_and_increments(self):
        original_version = self.exam.version
        MasterExamService.update(
            self.exam,
            {'name': 'Renamed'},
            expected_version=original_version,
        )
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.name, 'Renamed')
        self.assertEqual(self.exam.version, original_version + 1)

    def test_stale_version_raises_modified_by_another_user(self):
        MasterExamService.update(
            self.exam, {'name': 'First edit'},
            expected_version=self.exam.version,
        )
        self.exam.refresh_from_db()
        # Second caller still thinks the version is the original.
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.update(
                self.exam,
                {'name': 'Second edit'},
                expected_version=1,  # stale
            )
        self.assertEqual(str(ctx.exception), 'MODIFIED_BY_ANOTHER_USER')

    def test_missing_version_raises(self):
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.update(self.exam, {'name': 'X'})
        self.assertEqual(str(ctx.exception), 'MISSING_EXPECTED_VERSION')

    def test_window_started_raises(self):
        now = timezone.now()
        self.exam.opens_at = now - timedelta(hours=1)
        self.exam.closes_at = now + timedelta(hours=1)
        self.exam.save()
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.update(
                self.exam, {'name': 'Too late'},
                expected_version=self.exam.version,
            )
        self.assertEqual(str(ctx.exception), 'EXAM_WINDOW_STARTED')

    def test_empty_update_with_fresh_version_does_not_bump(self):
        """
        An update with no content fields is a CAS check, not a
        mutation. When the caller's version matches, the call
        succeeds and the version stays where it was.

        This is the actual behavior. Two callers who both observe
        version N and both send an empty update both succeed,
        because neither changed anything — the CAS only guards
        against changes, and there were none.
        """
        original = self.exam.version
        MasterExamService.update(
            self.exam, {}, expected_version=original,
        )
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.version, original)

    def test_empty_update_with_stale_version_still_raises(self):
        """
        The complement of the test above. The empty branch still
        runs a CAS — it just does not advance the version. A caller
        whose snapshot is stale is refused, even though they sent no
        content fields.
        """
        # Advance the version via a real content change.
        MasterExamService.update(
            self.exam, {'name': 'Changed'},
            expected_version=self.exam.version,
        )
        self.exam.refresh_from_db()
        current = self.exam.version

        with self.assertRaises(ValueError) as ctx:
            MasterExamService.update(
                self.exam, {}, expected_version=current - 1,
            )
        self.assertEqual(str(ctx.exception), 'MODIFIED_BY_ANOTHER_USER')

    def test_two_empty_updates_both_succeed(self):
        """
        Documents the consequence explicitly: two callers with the
        same snapshot can both send empty updates and both get a
        success. This is consistent with "nothing changed", and it
        is the reason the empty branch does not bump the version —
        a bump on every empty call would turn read-only checks into
        write conflicts.
        """
        version = self.exam.version
        MasterExamService.update(self.exam, {}, expected_version=version)
        MasterExamService.update(self.exam, {}, expected_version=version)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.version, version)


class UpdateAudienceReplacementTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.exam = _draft_exam(self.author)

    def test_audience_group_ids_replaces_set(self):
        from apps.groups.models import Group
        g1 = Group.objects.create(name='Group A', created_by='t')
        g2 = Group.objects.create(name='Group B', created_by='t')

        MasterExamService.update(
            self.exam, {'audience_group_ids': [g1.id]},
            expected_version=self.exam.version,
        )
        self.exam.refresh_from_db()
        self.assertEqual(
            set(self.exam.audience_groups.values_list('id', flat=True)),
            {g1.id},
        )

        MasterExamService.update(
            self.exam, {'audience_group_ids': [g2.id]},
            expected_version=self.exam.version,
        )
        self.exam.refresh_from_db()
        self.assertEqual(
            set(self.exam.audience_groups.values_list('id', flat=True)),
            {g2.id},
        )

    def test_audience_user_ids_excludes_primary_attending(self):
        other = make_user('other_user')
        MasterExamService.update(
            self.exam,
            {'audience_user_ids': [other.id, self.author.id]},
            expected_version=self.exam.version,
        )
        self.exam.refresh_from_db()
        audience_ids = set(
            self.exam.audience_users.values_list('id', flat=True)
        )
        self.assertIn(other.id, audience_ids)
        self.assertNotIn(self.author.id, audience_ids)

    def test_co_attending_ids_excludes_primary_attending(self):
        other = make_user('co_mod')
        MasterExamService.update(
            self.exam,
            {'co_attending_ids': [other.id, self.author.id]},
            expected_version=self.exam.version,
        )
        self.exam.refresh_from_db()
        co_ids = set(self.exam.co_attendings.values_list('id', flat=True))
        self.assertIn(other.id, co_ids)
        self.assertNotIn(self.author.id, co_ids)


class CreateAudiencePopulationTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')

    def _payload(self, **kw):
        now = timezone.now()
        base = {
            'name': 'New Exam',
            'opens_at': now + timedelta(hours=1),
            'closes_at': now + timedelta(hours=2),
            'duration_minutes': 60,
        }
        base.update(kw)
        return base

    def test_audience_all_doctors_flag(self):
        exam = MasterExamService.create(
            self.author,
            self._payload(audience_all_doctors=True),
        )
        self.assertTrue(exam.audience_all_doctors)

    def test_audience_groups_populated(self):
        from apps.groups.models import Group
        g = Group.objects.create(name='G', created_by='t')
        exam = MasterExamService.create(
            self.author,
            self._payload(audience_group_ids=[g.id]),
        )
        self.assertEqual(
            set(exam.audience_groups.values_list('id', flat=True)),
            {g.id},
        )

    def test_co_attendings_populated_and_exclude_author(self):
        co = make_user('co_mod')
        exam = MasterExamService.create(
            self.author,
            self._payload(co_attending_ids=[co.id, self.author.id]),
        )
        co_ids = set(exam.co_attendings.values_list('id', flat=True))
        self.assertIn(co.id, co_ids)
        self.assertNotIn(self.author.id, co_ids)

    def test_question_ids_populated_in_order(self):
        q1 = make_question(owner=self.author)
        q2 = make_question(owner=self.author)
        exam = MasterExamService.create(
            self.author,
            self._payload(question_ids=[q2.id, q1.id]),
        )
        ordered = list(
            exam.exam_questions
            .order_by('order')
            .values_list('question_id', flat=True)
        )
        self.assertEqual(ordered, [q2.id, q1.id])

    def test_invisible_question_rejected(self):
        other = make_user('other_author')
        other_draft = make_question(
            owner=other, is_draft=True, draft_owner=other,
        )
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.create(
                self.author,
                self._payload(question_ids=[other_draft.id]),
            )
        self.assertTrue(
            str(ctx.exception).startswith('QUESTIONS_NOT_ACCESSIBLE'),
        )


class AddQuestionsVisibilityTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.mod = make_user('mod_user', role='moderator')
        self.q1 = make_question(owner=self.mod)
        self.exam = _draft_exam(self.mod, [self.q1])

    def test_own_draft_can_be_added(self):
        own_draft = make_question(
            owner=self.mod, is_draft=True, draft_owner=self.mod,
        )
        MasterExamService.add_questions(
            self.exam, [own_draft.id], request=_req(self.mod),
        )
        self.assertEqual(self.exam.exam_questions.count(), 2)

    def test_other_users_draft_rejected(self):
        other = make_user('other_author')
        other_draft = make_question(
            owner=other, is_draft=True, draft_owner=other,
        )
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.add_questions(
                self.exam, [other_draft.id], request=_req(self.mod),
            )
        self.assertTrue(
            str(ctx.exception).startswith('QUESTIONS_NOT_ACCESSIBLE'),
        )

    def test_duplicate_ids_dropped(self):
        q2 = make_question(owner=self.mod)
        MasterExamService.add_questions(
            self.exam, [q2.id, q2.id], request=_req(self.mod),
        )
        self.assertEqual(self.exam.exam_questions.count(), 2)

    def test_existing_ids_dropped(self):
        MasterExamService.add_questions(
            self.exam, [self.q1.id], request=_req(self.mod),
        )
        self.assertEqual(self.exam.exam_questions.count(), 1)

    def test_window_started_raises(self):
        now = timezone.now()
        self.exam.opens_at = now - timedelta(hours=1)
        self.exam.closes_at = now + timedelta(hours=1)
        self.exam.save()
        q2 = make_question(owner=self.mod)
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.add_questions(
                self.exam, [q2.id], request=_req(self.mod),
            )
        self.assertEqual(str(ctx.exception), 'EXAM_WINDOW_STARTED')


class ReorderExtendedTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.mod = make_user('mod_user', role='moderator')
        self.q1 = make_question(owner=self.mod)
        self.q2 = make_question(owner=self.mod)
        self.exam = _draft_exam(self.mod, [self.q1, self.q2])

    def test_mismatch_raises(self):
        q3 = make_question(owner=self.mod)
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.reorder_questions(
                self.exam, [self.q1.id, q3.id],
            )
        self.assertEqual(str(ctx.exception), 'REORDER_MISMATCH')

    def test_non_numeric_id_raises(self):
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.reorder_questions(
                self.exam, ['not-a-number', self.q1.id],
            )
        self.assertEqual(str(ctx.exception), 'INVALID_QUESTION_IDS')

    def test_window_started_raises(self):
        now = timezone.now()
        self.exam.opens_at = now - timedelta(hours=1)
        self.exam.closes_at = now + timedelta(hours=1)
        self.exam.save()
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.reorder_questions(
                self.exam, [self.q2.id, self.q1.id],
            )
        self.assertEqual(str(ctx.exception), 'EXAM_WINDOW_STARTED')


class AddDraftExtendedTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.mod = make_user('mod_user', role='moderator')
        self.exam = _draft_exam(self.mod)

    def _payload(self, **kw):
        base = {
            'question': 'Inline draft?',
            'choices': ['A', 'B'],
            'correct_answer': 1,
        }
        base.update(kw)
        return base

    def test_draft_without_case(self):
        draft = MasterExamService.add_draft(
            self.exam, self._payload(), self.mod,
        )
        self.assertIsNone(draft.case)
        self.assertTrue(draft.is_draft)
        self.assertEqual(draft.draft_owner, self.mod)

    def test_draft_with_new_case_key(self):
        draft = MasterExamService.add_draft(
            self.exam,
            self._payload(case_key='new-case', case_stem='A vignette'),
            self.mod,
        )
        self.assertIsNotNone(draft.case_id)
        self.assertEqual(draft.case.key, 'new-case')
        self.assertEqual(draft.case.stem, 'A vignette')
        self.assertEqual(draft.case.authored_by, self.mod)

    def test_draft_with_existing_case_key(self):
        existing = make_case('existing-case', stem='old stem')
        draft = MasterExamService.add_draft(
            self.exam,
            self._payload(case_key='existing-case'),
            self.mod,
        )
        self.assertEqual(draft.case_id, existing.id)
        draft.case.refresh_from_db()
        self.assertEqual(draft.case.stem, 'old stem')

    def test_window_started_raises(self):
        now = timezone.now()
        self.exam.opens_at = now - timedelta(hours=1)
        self.exam.closes_at = now + timedelta(hours=1)
        self.exam.save()
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.add_draft(
                self.exam, self._payload(), self.mod,
            )
        self.assertEqual(str(ctx.exception), 'EXAM_WINDOW_STARTED')