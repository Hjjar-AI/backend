# tests/feedback/test_flag_attempt_link.py
"""
A QuestionFlag raised from within a master exam attempt carries
the attempt FK. The moderator dashboard uses that link to show
"raised during exam X". The baseline flag tests never set it.
"""
from datetime import timedelta

from django.utils import timezone

from apps.feedback.models import QuestionFlag
from apps.feedback.services import FeedbackService
from apps.master_exams.models import (
    MasterExam, MasterExamQuestion,
)
from apps.master_exams.services import MasterExamAttemptService
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


class FlagAttemptLinkTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.student = make_user('student')
        self.q = make_question(owner=self.author)

        now = timezone.now()
        self.exam = MasterExam.objects.create(
            name='Flag Test',
            primary_attending=self.author,
            opens_at=now - timedelta(hours=1),
            closes_at=now + timedelta(hours=1),
            duration_minutes=60,
            stored_status='published',
            audience_all_doctors=True,
        )
        MasterExamQuestion.objects.create(
            master_exam=self.exam, question=self.q, order=1,
        )
        self.attempt = MasterExamAttemptService.start(self.student, self.exam)

    def test_flag_from_exam_carries_attempt_fk(self):
        FeedbackService.flag_question(
            self.student.id, self.q.id,
            reason='unclear',
            master_exam_attempt=self.attempt,
        )
        flag = QuestionFlag.objects.get(question=self.q, user=self.student)
        self.assertEqual(flag.master_exam_attempt, self.attempt)

    def test_flag_without_attempt_has_null_fk(self):
        FeedbackService.flag_question(
            self.student.id, self.q.id, reason='general',
        )
        flag = QuestionFlag.objects.get(question=self.q, user=self.student)
        self.assertIsNone(flag.master_exam_attempt)

    def test_attempt_survives_flag_deletion(self):
        """
        The FK uses SET_NULL, so deleting the attempt does not
        cascade to the flag — the moderator still sees the report.
        """
        FeedbackService.flag_question(
            self.student.id, self.q.id,
            reason='x',
            master_exam_attempt=self.attempt,
        )
        flag = QuestionFlag.objects.get(question=self.q)
        self.attempt.delete()
        flag.refresh_from_db()
        self.assertIsNone(flag.master_exam_attempt)