# tests/learning/test_learning_service.py
from datetime import timedelta

from django.utils import timezone

from apps.learning.models import UserQuestionAttempt
from apps.learning.services import LearningService
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


class StudyNowQueueTests(CacheClearingTestCase):
    """
    BUDGET MODEL
    ------------
    `study_now_queue` allocates `limit` slots across five buckets
    with these initial budgets (for limit=N):

        srs_due          = int(N * 0.5)
        fragile          = int(N * 0.2)
        wrong_open       = int(N * 0.2)
        weak_categories  = N - the above three

    When a bucket under-fills, its deficit rolls into the NEXT
    bucket in the sequence srs_due → fragile → wrong_open →
    weak_categories. `weak_categories` is the last budgeted bucket;
    its deficit does not roll further. Remaining slots are then
    filled by the fresh-questions fallback.

    FRESH FALLBACK IS MANDATORY FOR total == limit
    ----------------------------------------------
    The cascade above cannot fill the tail on its own. Only the
    fresh fallback can reach `total == limit` when the earlier
    buckets are smaller than their budgets. Every test below that
    asserts `total == 10` must therefore seed enough never-attempted
    questions via `_make_fresh()`. `test_empty_question_bank_returns_empty`
    is the one test that deliberately does not — it asserts total == 0.

    BUCKET OVERLAP
    --------------
    A single UserQuestionAttempt row can be in more than one bucket.
    The defaults on the model make a fresh attempt:
      • last_correct=False, ever_correct=False  → wrong_open
      • last_confidence=True                    → NOT fragile

    So a naive "make 15 due questions" setup produces 15 rows that
    are BOTH srs_due AND wrong_open. After the SRS bucket pulls its
    5, the leftover 10 are still eligible for wrong_open — and if
    the fragile deficit has rolled into wrong_open, that bucket's
    budget has grown. The result is a test that passes or fails
    based on a subtle interaction between the buckets rather than on
    the bucket under test.

    The three helpers below construct explicit non-overlapping
    populations so each test isolates one bucket.
    """
    def setUp(self):
        super().setUp()
        self.u = make_user()

    # ── Helpers ────────────────────────────────────────────────────
    def _make_srs_only(self, n):
        """
        Questions that are due NOW and nothing else:
          • next_due in the past   → srs_due
          • last_correct=True      → NOT wrong_open
          • ever_correct=True      → NOT wrong_open
          • last_confidence=True   → NOT fragile
        """
        for _ in range(n):
            q = make_question(owner=self.u)
            UserQuestionAttempt.objects.create(
                user=self.u, question=q,
                next_due=timezone.now() - timedelta(days=1),
                last_correct=True,
                ever_correct=True,
                last_confidence=True,
            )

    def _make_fragile_only(self, n):
        """
        Questions that are fragile and nothing else:
          • last_correct=True, last_confidence=False → fragile
          • next_due far in the future               → NOT srs_due
          • ever_correct=True                        → NOT wrong_open
        """
        for _ in range(n):
            q = make_question(owner=self.u)
            UserQuestionAttempt.objects.create(
                user=self.u, question=q,
                last_correct=True,
                last_confidence=False,
                ever_correct=True,
                next_due=timezone.now() + timedelta(days=30),
            )

    def _make_wrong_open_only(self, n):
        """
        Questions that are wrong-open and nothing else:
          • ever_correct=False, last_correct=False  → wrong_open
          • next_due far in the future              → NOT srs_due
          • last_confidence=True                    → NOT fragile
        """
        for _ in range(n):
            q = make_question(owner=self.u)
            UserQuestionAttempt.objects.create(
                user=self.u, question=q,
                ever_correct=False,
                last_correct=False,
                last_confidence=True,
                next_due=timezone.now() + timedelta(days=30),
            )

    def _make_fresh(self, n):
        """Never-attempted public questions."""
        for _ in range(n):
            make_question(owner=self.u)

    # ── Tests ──────────────────────────────────────────────────────

    def test_anonymous_returns_empty_payload(self):
        from django.contrib.auth.models import AnonymousUser
        result = LearningService.study_now_queue(AnonymousUser())
        self.assertEqual(result['question_ids'], [])
        self.assertEqual(result['total'], 0)
        self.assertEqual(
            set(result['breakdown'].keys()),
            {'srs_due', 'fragile', 'wrong_open', 'weak_categories', 'fresh'},
        )

    def test_brand_new_user_gets_fresh_questions(self):
        self._make_fresh(30)
        result = LearningService.study_now_queue(self.u, limit=10)
        self.assertEqual(result['total'], 10)
        self.assertEqual(result['breakdown']['fresh'], 10)

    def test_empty_question_bank_returns_empty(self):
        result = LearningService.study_now_queue(self.u, limit=10)
        self.assertEqual(result['question_ids'], [])
        self.assertEqual(result['total'], 0)

    def test_srs_due_bucket_fills_its_budget(self):
        """
        For limit=10 the SRS budget is 5. With 15 SRS-only questions
        and 20 fresh, SRS takes its full 5 (deficit 0). Fragile has
        no candidates; its 2-slot budget rolls to wrong_open, which
        also has none; wrong_open's 4 slots roll to weak_categories,
        which is empty. The 5 unfilled slots fall through to fresh.
        """
        self._make_srs_only(15)
        self._make_fresh(20)

        result = LearningService.study_now_queue(self.u, limit=10)
        self.assertEqual(result['breakdown']['srs_due'], 5)
        self.assertEqual(result['breakdown']['fragile'], 0)
        self.assertEqual(result['breakdown']['wrong_open'], 0)
        self.assertEqual(result['breakdown']['weak_categories'], 0)
        self.assertEqual(result['breakdown']['fresh'], 5)
        self.assertEqual(result['total'], 10)

    def test_srs_shortfall_rolls_into_fragile(self):
        """
        For limit=10 the SRS budget is 5 and the fragile budget is 2.
        With only 2 SRS-only questions, the 3-slot shortfall rolls
        into fragile, bringing it to 5. Fragile has 20 candidates and
        fills its 5 with 0 deficit. The remaining cascade finds no
        wrong_open or weak_categories candidates, so the last 3 slots
        come from fresh.
        """
        self._make_srs_only(2)
        self._make_fragile_only(20)
        self._make_fresh(20)

        result = LearningService.study_now_queue(self.u, limit=10)
        self.assertEqual(result['breakdown']['srs_due'], 2)
        # 2 (base) + 3 (rollover) = 5.
        self.assertEqual(result['breakdown']['fragile'], 5)
        self.assertEqual(result['breakdown']['wrong_open'], 0)
        self.assertEqual(result['breakdown']['weak_categories'], 0)
        self.assertEqual(result['breakdown']['fresh'], 3)
        self.assertEqual(result['total'], 10)

    def test_wrong_open_absorbs_rollover_when_fragile_empty(self):
        """
        With no SRS and no fragile candidates, the entire prefix of
        the budget (5 + 2) rolls into wrong_open, bringing it to 9.
        Wrong_open has 20 candidates; it fills 9. The last 1 slot
        comes from fresh.
        """
        self._make_wrong_open_only(20)
        self._make_fresh(20)

        result = LearningService.study_now_queue(self.u, limit=10)
        self.assertEqual(result['breakdown']['srs_due'], 0)
        self.assertEqual(result['breakdown']['fragile'], 0)
        # 2 (base) + 5 (srs deficit) + 2 (fragile deficit) = 9.
        self.assertEqual(result['breakdown']['wrong_open'], 9)
        self.assertEqual(result['breakdown']['weak_categories'], 0)
        self.assertEqual(result['breakdown']['fresh'], 1)
        self.assertEqual(result['total'], 10)

    def test_never_returns_duplicates(self):
        # A question that is BOTH due and fragile — must appear once.
        q = make_question(owner=self.u)
        UserQuestionAttempt.objects.create(
            user=self.u, question=q,
            next_due=timezone.now() - timedelta(days=1),
            last_correct=True, last_confidence=False,
        )
        self._make_fresh(20)
        result = LearningService.study_now_queue(self.u, limit=10)
        ids = result['question_ids']
        self.assertEqual(len(ids), len(set(ids)))

    def test_limit_is_clamped_to_200(self):
        self._make_fresh(300)
        result = LearningService.study_now_queue(self.u, limit=1000)
        self.assertLessEqual(result['total'], 200)

    def test_zero_limit_is_clamped_to_one(self):
        """
        The service clamps limit to the range [1, 200]. A limit of 0
        (or any negative value) must produce exactly one question
        when at least one question is visible — not zero, which is
        what an unclamped implementation would produce.

        The previous version of this test asserted `>= 0`, which is
        trivially true for a list length and would still pass if the
        clamp were removed entirely.
        """
        make_question(owner=self.u)
        result = LearningService.study_now_queue(self.u, limit=0)
        self.assertEqual(result['total'], 1)

    def test_negative_limit_is_clamped_to_one(self):
        make_question(owner=self.u)
        result = LearningService.study_now_queue(self.u, limit=-5)
        self.assertEqual(result['total'], 1)

    def test_invalid_limit_uses_default(self):
        self._make_fresh(30)
        result = LearningService.study_now_queue(self.u, limit='garbage')
        self.assertLessEqual(result['total'], 20)