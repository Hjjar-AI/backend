# tests/feedback/test_feedback_service.py
from apps.feedback.models import Bookmark, QuestionFlag
from apps.feedback.services import FeedbackService
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


class BookmarkToggleTests(CacheClearingTestCase):
    def test_first_toggle_creates(self):
        u = make_user()
        q = make_question(owner=u)
        added = FeedbackService.toggle_bookmark(u.id, q.id)
        self.assertTrue(added)
        self.assertEqual(Bookmark.objects.count(), 1)

    def test_second_toggle_removes(self):
        u = make_user()
        q = make_question(owner=u)
        FeedbackService.toggle_bookmark(u.id, q.id)
        added = FeedbackService.toggle_bookmark(u.id, q.id)
        self.assertFalse(added)
        self.assertEqual(Bookmark.objects.count(), 0)


class FlagQuestionTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.u = make_user()
        self.q = make_question(owner=self.u)

    def test_first_flag_creates(self):
        self.assertTrue(
            FeedbackService.flag_question(self.u.id, self.q.id, 'reason'),
        )

    def test_second_open_flag_is_refused(self):
        FeedbackService.flag_question(self.u.id, self.q.id, 'first')
        self.assertFalse(
            FeedbackService.flag_question(self.u.id, self.q.id, 'second'),
        )
        self.assertEqual(
            QuestionFlag.objects.filter(resolved=False).count(), 1,
        )

    def test_resolved_flag_allows_a_new_one(self):
        """
        A resolved flag does not count against the partial unique
        constraint (condition=Q(resolved=False)). The same user may
        flag again if a moderator reopened the issue.
        """
        FeedbackService.flag_question(self.u.id, self.q.id, 'first')
        QuestionFlag.objects.update(resolved=True)
        self.assertTrue(
            FeedbackService.flag_question(self.u.id, self.q.id, 'second'),
        )
        self.assertEqual(QuestionFlag.objects.count(), 2)

    def test_different_users_have_independent_flags(self):
        other = make_user()
        self.assertTrue(
            FeedbackService.flag_question(self.u.id, self.q.id, ''),
        )
        self.assertTrue(
            FeedbackService.flag_question(other.id, self.q.id, ''),
        )
        self.assertEqual(QuestionFlag.objects.count(), 2)