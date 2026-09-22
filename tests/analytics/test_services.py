# tests/analytics/test_services.py
"""
Service-layer tests for the member-facing and member-advanced
analytics methods.

This file is NOT a duplicate of test_cohort_comparison.py. The
cohort-comparison tests (admin-advanced feature 7) live there;
these cover:

  • member.py            — get_category_coverage, get_tag_coverage,
                           get_difficulty_stats,
                           get_user_performance_trend,
                           get_user_weak_categories,
                           get_confidence_stats,
                           get_active_users_stats
  • member_advanced.py   — get_category_mastery, get_streak_history

The HTTP-layer tests in tests/api/test_analytics_api.py assert the
wire shape of these endpoints. These tests assert the service
semantics directly, so a regression in the computation is caught
even if the serializer keeps the shape stable.

AN EARLIER REVISION OF THIS FILE WAS A DUPLICATE
------------------------------------------------
`tests/analytics/test_services.py` and
`tests/analytics/test_cohort_comparison.py` used to be
byte-identical — same docstring, same helpers, same
`CohortComparisonTests` class. Whichever was created second was
never differentiated, so the entire cohort-comparison suite ran
twice under the same class name in two modules. The duplicate has
been removed; the content below is new.

SEMANTIC NOTE ON `wrong_open`
-----------------------------
`get_confidence_stats` counts `wrong_open` as `ever_correct=False`
— a question the user has NEVER gotten right. This is a different
predicate from `correct_fragile` (`last_correct=True AND
last_confidence=False`), and the two are independent. A test that
sets `last_correct=True` on a row WITHOUT also setting
`ever_correct=True` will have that row counted in BOTH buckets.
The ConfidenceStats test below sets both fields explicitly.
"""
from datetime import timedelta

from django.utils import timezone

from apps.analytics.services import AnalyticsService
from apps.learning.models import UserQuestionAttempt
from tests.base import CacheClearingTestCase
from tests.factories import (
    make_user,
    make_question,
    make_category,
    make_tag,
    make_test_history,
)


# ═══════════════════════════════════════════════════════════════════════
# member.py — global coverage reports
# ═══════════════════════════════════════════════════════════════════════

class CategoryCoverageTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')

    def test_empty_bank_returns_empty_list(self):
        self.assertEqual(AnalyticsService.get_category_coverage(), [])

    def test_counts_split_public_and_verified(self):
        cat = make_category('cat-a')
        make_question(owner=self.author, category=cat, verified=True)
        make_question(owner=self.author, category=cat, verified=False)
        make_question(
            owner=self.author, category=cat, verified=True,
            is_draft=True, draft_owner=self.author,
        )

        rows = AnalyticsService.get_category_coverage()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['category_id'], cat.id)
        self.assertEqual(row['category_name'], 'cat-a')
        # Draft is excluded from both `total` and `verified`.
        self.assertEqual(row['total'], 2)
        self.assertEqual(row['verified'], 1)

    def test_avg_accuracy_ignores_unanswered(self):
        cat = make_category('cat-a')
        # One answered question at 50%, one never answered.
        answered = make_question(owner=self.author, category=cat)
        answered.times_answered = 10
        answered.times_correct = 5
        answered.save()
        make_question(owner=self.author, category=cat)

        rows = AnalyticsService.get_category_coverage()
        # The unanswered question contributes no ratio; the
        # answered one contributes exactly 0.5.
        self.assertAlmostEqual(rows[0]['avg_accuracy'], 0.5, places=3)


class TagCoverageTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')

    def test_counts_public_and_verified_per_tag(self):
        tag = make_tag('tag-a')
        q_public = make_question(owner=self.author, verified=True)
        q_public.tags.add(tag)
        q_unverified = make_question(owner=self.author, verified=False)
        q_unverified.tags.add(tag)
        q_draft = make_question(
            owner=self.author,
            is_draft=True, draft_owner=self.author,
        )
        q_draft.tags.add(tag)

        rows = AnalyticsService.get_tag_coverage()
        row = next(r for r in rows if r['tag_name'] == 'tag-a')
        # Draft excluded from both counts.
        self.assertEqual(row['count'], 2)
        self.assertEqual(row['verified_count'], 1)

    def test_tag_with_only_drafts_is_counted_as_zero(self):
        tag = make_tag('draft-only')
        q = make_question(
            owner=self.author,
            is_draft=True, draft_owner=self.author,
        )
        q.tags.add(tag)

        rows = AnalyticsService.get_tag_coverage()
        row = next(r for r in rows if r['tag_name'] == 'draft-only')
        self.assertEqual(row['count'], 0)
        self.assertEqual(row['verified_count'], 0)


class DifficultyStatsTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')

    def test_each_difficulty_bucket_reported(self):
        for diff in ('easy', 'medium', 'hard'):
            make_question(owner=self.author, difficulty=diff)

        rows = AnalyticsService.get_difficulty_stats()
        by_diff = {r['difficulty']: r for r in rows}
        self.assertEqual(set(by_diff), {'easy', 'medium', 'hard'})
        for r in by_diff.values():
            self.assertEqual(r['count'], 1)

    def test_draft_excluded(self):
        make_question(
            owner=self.author, difficulty='easy',
            is_draft=True, draft_owner=self.author,
        )
        rows = AnalyticsService.get_difficulty_stats()
        by_diff = {r['difficulty']: r for r in rows}
        # No rows at all — the only question was a draft.
        self.assertEqual(by_diff, {})


# ═══════════════════════════════════════════════════════════════════════
# member.py — per-user reports
# ═══════════════════════════════════════════════════════════════════════

class UserPerformanceTrendTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('alice')

    def test_empty_for_user_with_no_sessions(self):
        self.assertEqual(
            AnalyticsService.get_user_performance_trend(self.user.id), [],
        )

    def test_trend_returns_one_row_per_day(self):
        now = timezone.now()
        make_test_history(
            self.user, accuracy=60.0, total_questions=10,
            completed_at=now,
        )
        make_test_history(
            self.user, accuracy=80.0, total_questions=5,
            completed_at=now - timedelta(days=1),
        )

        rows = AnalyticsService.get_user_performance_trend(self.user.id, days=7)
        self.assertEqual(len(rows), 2)
        # Ascending by date.
        self.assertLess(rows[0]['date'], rows[1]['date'])

    def test_same_day_sessions_aggregated(self):
        now = timezone.now()
        make_test_history(self.user, accuracy=60.0, completed_at=now)
        make_test_history(self.user, accuracy=80.0, completed_at=now)

        rows = AnalyticsService.get_user_performance_trend(self.user.id, days=7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['accuracy'], 70.0)


class UserWeakCategoriesTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('alice')

    def test_min_attempts_filters_low_volume(self):
        cat = make_category('cat-a')
        q = make_question(owner=self.user, category=cat)
        UserQuestionAttempt.objects.create(
            user=self.user, question=q, attempts=2, wrong_count=2,
        )
        # min_attempts defaults to 3, so this category is filtered out.
        self.assertEqual(
            AnalyticsService.get_user_weak_categories(self.user.id), [],
        )

    def test_ranks_lowest_accuracy_first(self):
        cat_a = make_category('cat-a')
        cat_b = make_category('cat-b')
        qa = make_question(owner=self.user, category=cat_a)
        qb = make_question(owner=self.user, category=cat_b)
        UserQuestionAttempt.objects.create(
            user=self.user, question=qa, attempts=10, wrong_count=8,
        )
        UserQuestionAttempt.objects.create(
            user=self.user, question=qb, attempts=10, wrong_count=2,
        )

        rows = AnalyticsService.get_user_weak_categories(self.user.id)
        self.assertEqual(rows[0]['category_name'], 'cat-a')
        self.assertLess(rows[0]['accuracy'], rows[1]['accuracy'])

    def test_top_caps_the_result(self):
        for i in range(5):
            cat = make_category(f'cat-{i}')
            q = make_question(owner=self.user, category=cat)
            UserQuestionAttempt.objects.create(
                user=self.user, question=q, attempts=10, wrong_count=5,
            )
        rows = AnalyticsService.get_user_weak_categories(
            self.user.id, top=3,
        )
        self.assertEqual(len(rows), 3)


class ConfidenceStatsTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('alice')

    def test_empty_user_returns_zero_payload(self):
        data = AnalyticsService.get_confidence_stats(self.user.id)
        self.assertEqual(data['total_attempted'], 0)
        self.assertEqual(data['fragile_ratio'], 0.0)

    def test_fragile_ratio_computed_over_correct_answers(self):
        """
        `wrong_open` is `ever_correct=False` — a question the user
        has NEVER gotten right. `correct_fragile` is
        `last_correct=True AND last_confidence=False`. The two
        predicates are independent.

        This test's earlier revision forgot to set `ever_correct=True`
        on the two correct rows, so they defaulted to `False` and
        were also counted as wrong_open. The service returned 3 —
        correct per its own definition — and the assertion was the
        bug.
        """
        q1 = make_question(owner=self.user)
        q2 = make_question(owner=self.user)
        q3 = make_question(owner=self.user)

        # Correct-and-confident, and ever_correct=True so it is NOT
        # wrong_open.
        UserQuestionAttempt.objects.create(
            user=self.user, question=q1,
            last_correct=True, last_confidence=True, ever_correct=True,
        )
        # Correct-but-fragile. ever_correct=True for the same reason.
        UserQuestionAttempt.objects.create(
            user=self.user, question=q2,
            last_correct=True, last_confidence=False, ever_correct=True,
        )
        # Wrong and never corrected.
        UserQuestionAttempt.objects.create(
            user=self.user, question=q3,
            last_correct=False, last_confidence=True, ever_correct=False,
        )

        data = AnalyticsService.get_confidence_stats(self.user.id)
        self.assertEqual(data['total_attempted'], 3)
        self.assertEqual(data['correct_confident'], 1)
        self.assertEqual(data['correct_fragile'], 1)
        self.assertEqual(data['wrong_open'], 1)
        # 1 fragile out of 2 correct = 50%.
        self.assertEqual(data['fragile_ratio'], 50.0)


class ActiveUsersStatsTests(CacheClearingTestCase):
    def test_returns_list_shape(self):
        # The service returns a list of per-day dicts. Empty data is a
        # legal input — a fresh install with no logins in the window.
        rows = AnalyticsService.get_active_users_stats(days=30)
        self.assertIsInstance(rows, list)

    def test_active_user_appears_on_their_login_day(self):
        now = timezone.now()
        u = make_user('alice')
        u.last_login = now
        u.save()

        rows = AnalyticsService.get_active_users_stats(days=30)
        today = now.date().isoformat()
        today_row = next((r for r in rows if r['date'] == today), None)
        self.assertIsNotNone(today_row)
        self.assertEqual(today_row['active_users'], 1)


# ═══════════════════════════════════════════════════════════════════════
# member_advanced.py — features 1 and 3
# ═══════════════════════════════════════════════════════════════════════

class CategoryMasteryTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('alice')

    def test_empty_returns_empty_payload(self):
        data = AnalyticsService.get_category_mastery(self.user.id)
        self.assertEqual(data['categories'], [])
        self.assertEqual(data['total_categories'], 0)

    def test_mastered_flag_at_eighty_percent(self):
        cat = make_category('strong')
        q = make_question(owner=self.user, category=cat)
        UserQuestionAttempt.objects.create(
            user=self.user, question=q, attempts=10, wrong_count=2,
        )
        rows = AnalyticsService.get_category_mastery(self.user.id)
        row = rows['categories'][0]
        self.assertEqual(row['accuracy'], 80.0)
        self.assertTrue(row['mastered'])

    def test_seventy_nine_percent_is_not_mastered(self):
        cat = make_category('almost')
        q = make_question(owner=self.user, category=cat)
        # 79/100 ≈ 79.0%
        UserQuestionAttempt.objects.create(
            user=self.user, question=q, attempts=100, wrong_count=21,
        )
        rows = AnalyticsService.get_category_mastery(self.user.id)
        self.assertFalse(rows['categories'][0]['mastered'])

    def test_ranks_highest_accuracy_first(self):
        strong = make_category('strong')
        weak = make_category('weak')
        qs = make_question(owner=self.user, category=strong)
        qw = make_question(owner=self.user, category=weak)
        UserQuestionAttempt.objects.create(
            user=self.user, question=qs, attempts=10, wrong_count=1,
        )
        UserQuestionAttempt.objects.create(
            user=self.user, question=qw, attempts=10, wrong_count=8,
        )
        rows = AnalyticsService.get_category_mastery(self.user.id)
        self.assertEqual(rows['categories'][0]['category_name'], 'strong')


class StreakHistoryTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('alice')

    def test_default_window_is_thirty_days(self):
        data = AnalyticsService.get_streak_history(self.user.id)
        self.assertEqual(len(data['days']), 30)

    def test_days_param_clamped_to_min_seven(self):
        data = AnalyticsService.get_streak_history(self.user.id, days=1)
        self.assertEqual(len(data['days']), 7)

    def test_days_param_clamped_to_max_365(self):
        data = AnalyticsService.get_streak_history(self.user.id, days=9999)
        self.assertEqual(len(data['days']), 365)

    def test_session_landing_on_today_updates_count(self):
        make_test_history(self.user, total_questions=4)
        data = AnalyticsService.get_streak_history(self.user.id, days=7)
        today = timezone.localdate().isoformat()
        today_row = next(d for d in data['days'] if d['date'] == today)
        self.assertEqual(today_row['questions'], 4)
        self.assertEqual(today_row['sessions'], 1)

    def test_days_list_is_contiguous(self):
        from datetime import date
        data = AnalyticsService.get_streak_history(self.user.id, days=14)
        parsed = [date.fromisoformat(d['date']) for d in data['days']]
        for a, b in zip(parsed, parsed[1:]):
            self.assertEqual((b - a).days, 1)

    def test_streak_fields_read_from_user_model(self):
        self.user.current_streak = 4
        self.user.longest_streak = 9
        self.user.save()
        data = AnalyticsService.get_streak_history(self.user.id)
        self.assertEqual(data['current_streak'], 4)
        self.assertEqual(data['longest_streak'], 9)