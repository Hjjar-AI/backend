# tests/master_exams/test_master_exam_lifecycle.py
"""
Publish / cancel / publish-to-bank / delete at the service layer.

The three delete_mode branches (delete_drafts, keep_drafts,
publish_drafts) are the highest-value part of this file — the
HTTP-layer tests exercise publish and cancel, but the delete modes
have no coverage anywhere else.

AUDIT LOGGING IS CONDITIONAL ON `request=`
------------------------------------------
Every service method that writes an audit row guards the write with
`if request is not None`. This distinguishes a user-initiated action
from a framework-driven one: a `manage.py shell` call to
`MasterExamService.publish(exam)` produces no `PrivilegedAction` row
because there is no actor to attribute it to. The two audit tests
below pin both directions of that contract.
"""
from datetime import timedelta

from django.test import RequestFactory
from django.utils import timezone

from apps.master_exams.models import (
    MasterExam, MasterExamQuestion,
)
from apps.master_exams.services import MasterExamService
from apps.master_exams.services.master_exam_service.constants import (
    DELETE_MODE_DELETE,
    DELETE_MODE_KEEP,
    DELETE_MODE_PUBLISH,
)
from apps.questions.models import Question, Tag
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


def _draft_exam(author, questions=(), **kw):
    now = timezone.now()
    defaults = {
        'name': 'Lifecycle Exam',
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


def _req(user):
    """
    Build a RequestFactory request carrying `user`. The service
    layer's audit hook reads `request.user` and `request.META`; a
    plain RequestFactory request has META, we just attach the user.
    """
    req = RequestFactory().post('/')
    req.user = user
    return req


class PublishTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.q = make_question(owner=self.author)

    def test_publish_draft_with_questions(self):
        exam = _draft_exam(self.author, [self.q])
        MasterExamService.publish(exam)
        exam.refresh_from_db()
        self.assertEqual(exam.stored_status, 'published')
        self.assertIsNotNone(exam.published_at)

    def test_publish_empty_exam_refused(self):
        exam = _draft_exam(self.author)
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.publish(exam)
        self.assertEqual(str(ctx.exception), 'EMPTY_EXAM')

    def test_publish_already_published_refused(self):
        exam = _draft_exam(self.author, [self.q])
        MasterExamService.publish(exam)
        exam.refresh_from_db()
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.publish(exam)
        self.assertEqual(str(ctx.exception), 'EXAM_ALREADY_PUBLISHED')

    def test_publish_logged_to_audit(self):
        """
        The audit hook fires only when `request=` is passed. That is
        the contract the views rely on — a service call without a
        request is "the framework did this", and is deliberately not
        attributed to any actor.
        """
        from apps.core.models import PrivilegedAction
        exam = _draft_exam(self.author, [self.q])
        MasterExamService.publish(exam, request=_req(self.author))
        self.assertTrue(
            PrivilegedAction.objects.filter(
                action='master_exam.publish',
                actor=self.author,
            ).exists(),
        )

    def test_publish_without_request_does_not_write_audit(self):
        """
        Complement to the test above: a service call without a
        request must NOT write an audit row. This documents the
        contract so a future refactor cannot silently attribute
        framework-driven state changes to a random user.
        """
        from apps.core.models import PrivilegedAction
        exam = _draft_exam(self.author, [self.q])
        MasterExamService.publish(exam)
        self.assertFalse(
            PrivilegedAction.objects.filter(
                action='master_exam.publish',
            ).exists(),
        )


class CancelTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.q = make_question(owner=self.author)

    def test_cancel_draft(self):
        exam = _draft_exam(self.author, [self.q])
        MasterExamService.cancel(exam)
        exam.refresh_from_db()
        self.assertEqual(exam.stored_status, 'cancelled')

    def test_cancel_published(self):
        exam = _draft_exam(self.author, [self.q])
        MasterExamService.publish(exam)
        exam.refresh_from_db()
        MasterExamService.cancel(exam)
        exam.refresh_from_db()
        self.assertEqual(exam.stored_status, 'cancelled')

    def test_cancel_already_cancelled_refused(self):
        exam = _draft_exam(self.author, [self.q])
        MasterExamService.cancel(exam)
        exam.refresh_from_db()
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.cancel(exam)
        self.assertEqual(str(ctx.exception), 'EXAM_TERMINAL')

    def test_cancel_published_to_bank_refused(self):
        exam = _draft_exam(
            self.author, [self.q],
            opens_at=timezone.now() - timedelta(hours=2),
            closes_at=timezone.now() - timedelta(hours=1),
        )
        MasterExamService.publish(exam)
        exam.refresh_from_db()
        MasterExamService.publish_to_bank(exam)
        exam.refresh_from_db()
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.cancel(exam)
        self.assertEqual(str(ctx.exception), 'EXAM_TERMINAL')


class PublishToBankTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.q = make_question(
            owner=self.author, difficulty='medium',
            is_draft=True, draft_owner=self.author,
        )

    def test_publish_to_bank_requires_window_closed(self):
        exam = _draft_exam(self.author, [self.q])
        MasterExamService.publish(exam)
        exam.refresh_from_db()
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.publish_to_bank(exam)
        self.assertEqual(str(ctx.exception), 'WINDOW_NOT_CLOSED')

    def test_publish_to_bank_flips_drafts_to_public(self):
        exam = _draft_exam(
            self.author, [self.q],
            opens_at=timezone.now() - timedelta(hours=2),
            closes_at=timezone.now() - timedelta(hours=1),
            exam_topic_tag='depression',
        )
        MasterExamService.publish(exam)
        exam.refresh_from_db()
        MasterExamService.publish_to_bank(exam)

        self.q.refresh_from_db()
        self.assertFalse(self.q.is_draft)
        self.assertIsNone(self.q.draft_owner_id)

    def test_publish_to_bank_creates_topic_tag(self):
        exam = _draft_exam(
            self.author, [self.q],
            opens_at=timezone.now() - timedelta(hours=2),
            closes_at=timezone.now() - timedelta(hours=1),
            exam_topic_tag='depression',
        )
        MasterExamService.publish(exam)
        exam.refresh_from_db()
        MasterExamService.publish_to_bank(exam)

        # Tag name format: "{YYYY-MM-DD} - امتحان - {topic}"
        date_str = timezone.localtime(exam.opens_at).strftime('%Y-%m-%d')
        expected = f'{date_str} - امتحان - depression'
        self.assertTrue(Tag.objects.filter(name=expected).exists())
        self.q.refresh_from_db()
        self.assertIn(
            expected, list(self.q.tags.values_list('name', flat=True)),
        )

    def test_publish_to_bank_no_tag_falls_back_to_placeholder(self):
        exam = _draft_exam(
            self.author, [self.q],
            opens_at=timezone.now() - timedelta(hours=2),
            closes_at=timezone.now() - timedelta(hours=1),
            exam_topic_tag=None,
        )
        MasterExamService.publish(exam)
        exam.refresh_from_db()
        MasterExamService.publish_to_bank(exam)

        date_str = timezone.localtime(exam.opens_at).strftime('%Y-%m-%d')
        expected = f'{date_str} - امتحان - بدون عنوان'
        self.assertTrue(Tag.objects.filter(name=expected).exists())

    def test_publish_to_bank_already_published_refused(self):
        exam = _draft_exam(
            self.author, [self.q],
            opens_at=timezone.now() - timedelta(hours=2),
            closes_at=timezone.now() - timedelta(hours=1),
        )
        MasterExamService.publish(exam)
        exam.refresh_from_db()
        MasterExamService.publish_to_bank(exam)
        exam.refresh_from_db()
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.publish_to_bank(exam)
        self.assertEqual(str(ctx.exception), 'ALREADY_PUBLISHED_TO_BANK')


class DeleteModeTests(CacheClearingTestCase):
    """
    The three delete modes have visibly different side effects on
    the questions that were in the exam.
    """
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.draft_q = make_question(
            owner=self.author, question='Draft in exam?',
            is_draft=True, draft_owner=self.author,
        )
        self.public_q = make_question(
            owner=self.author, question='Public in exam?',
        )
        self.exam = _draft_exam(
            self.author, [self.draft_q, self.public_q],
        )

    def test_invalid_delete_mode_refused(self):
        with self.assertRaises(ValueError) as ctx:
            MasterExamService.delete(self.exam, 'not_a_mode')
        self.assertEqual(str(ctx.exception), 'INVALID_DELETE_MODE')

    def test_delete_mode_delete_drafts_removes_drafts(self):
        draft_id = self.draft_q.id
        public_id = self.public_q.id

        MasterExamService.delete(self.exam, DELETE_MODE_DELETE)

        self.assertFalse(Question.objects.filter(id=draft_id).exists())
        # Public question is not a draft, so it is untouched.
        self.assertTrue(Question.objects.filter(id=public_id).exists())
        # Exam itself is gone.
        self.assertFalse(
            MasterExam.objects.filter(id=self.exam.id).exists(),
        )

    def test_delete_mode_keep_drafts_leaves_questions_alone(self):
        draft_id = self.draft_q.id
        public_id = self.public_q.id

        MasterExamService.delete(self.exam, DELETE_MODE_KEEP)

        self.assertTrue(Question.objects.filter(id=draft_id).exists())
        self.assertTrue(Question.objects.filter(id=public_id).exists())
        self.draft_q.refresh_from_db()
        self.assertTrue(self.draft_q.is_draft)
        self.assertFalse(
            MasterExam.objects.filter(id=self.exam.id).exists(),
        )

    def test_delete_mode_publish_drafts_flips_them_public(self):
        draft_id = self.draft_q.id
        public_id = self.public_q.id

        MasterExamService.delete(self.exam, DELETE_MODE_PUBLISH)

        self.draft_q.refresh_from_db()
        self.assertFalse(self.draft_q.is_draft)
        self.assertIsNone(self.draft_q.draft_owner_id)
        self.assertTrue(Question.objects.filter(id=public_id).exists())
        self.assertFalse(
            MasterExam.objects.filter(id=self.exam.id).exists(),
        )

    def test_delete_mode_publish_also_tags_questions(self):
        self.exam.exam_topic_tag = 'lifecycle'
        self.exam.save(update_fields=['exam_topic_tag'])

        MasterExamService.delete(self.exam, DELETE_MODE_PUBLISH)

        date_str = timezone.localtime(self.exam.opens_at).strftime('%Y-%m-%d')
        expected = f'{date_str} - امتحان - lifecycle'
        self.assertTrue(Tag.objects.filter(name=expected).exists())

    def test_cannot_delete_published_to_bank(self):
        exam = _draft_exam(
            self.author, [self.public_q],
            opens_at=timezone.now() - timedelta(hours=2),
            closes_at=timezone.now() - timedelta(hours=1),
        )
        MasterExamService.publish(exam)
        exam.refresh_from_db()
        MasterExamService.publish_to_bank(exam)
        exam.refresh_from_db()

        with self.assertRaises(ValueError) as ctx:
            MasterExamService.delete(exam, DELETE_MODE_KEEP)
        self.assertEqual(str(ctx.exception), 'CANNOT_DELETE_PUBLISHED_TO_BANK')