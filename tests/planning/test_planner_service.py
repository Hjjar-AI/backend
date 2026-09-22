# tests/planning/test_planner_service.py
from datetime import timedelta

from django.utils import timezone

from apps.exams.models import TestHistory
from apps.planning.models import StudyPlanner, StudyPlannerDay
from apps.planning.services import StudyPlannerService
from apps.questions.models import Tag
from tests.base import CacheClearingTestCase
from tests.factories import (
    make_user, make_category, make_test_history,
)


def _today():
    return timezone.localdate()


class GetOrCreatePlannerTests(CacheClearingTestCase):
    def test_creates_with_defaults(self):
        u = make_user()
        p = StudyPlannerService.get_or_create_planner(u)
        self.assertEqual(p.user, u)
        self.assertEqual(p.target_questions_per_day, 10)

    def test_idempotent(self):
        u = make_user()
        a = StudyPlannerService.get_or_create_planner(u)
        b = StudyPlannerService.get_or_create_planner(u)
        self.assertEqual(a.pk, b.pk)


class UpdatePlannerTests(CacheClearingTestCase):
    """
    NOTE: `StudyPlanner.start_date` is a NOT NULL DateField whose
    default only applies at insert time. The service assigns whatever
    it is given straight to the field on every save, so a caller
    that passes `None` reaches the DB with a null and gets a
    constraint error. The HTTP view always substitutes
    `timezone.localdate()`, so this only surfaces on direct service
    calls. The tests below pass a real date.
    """
    def setUp(self):
        super().setUp()
        self.u = make_user()

    def test_replaces_categories_entirely(self):
        cat_a = make_category('cat-a')
        cat_b = make_category('cat-b')

        StudyPlannerService.update_planner(
            self.u, 10, [cat_a.id, cat_b.id], [], _today(), None,
        )
        p = StudyPlanner.objects.get(user=self.u)
        self.assertEqual(
            set(p.target_categories.values_list('id', flat=True)),
            {cat_a.id, cat_b.id},
        )

        StudyPlannerService.update_planner(
            self.u, 10, [cat_a.id], [], _today(), None,
        )
        p.refresh_from_db()
        self.assertEqual(
            set(p.target_categories.values_list('id', flat=True)),
            {cat_a.id},
        )

    def test_empty_categories_clears(self):
        cat = make_category('cat-a')
        StudyPlannerService.update_planner(
            self.u, 10, [cat.id], [], _today(), None,
        )
        StudyPlannerService.update_planner(
            self.u, 10, [], [], _today(), None,
        )
        p = StudyPlanner.objects.get(user=self.u)
        self.assertEqual(p.target_categories.count(), 0)

    def test_unknown_category_ids_are_dropped(self):
        cat = make_category('valid')
        StudyPlannerService.update_planner(
            self.u, 10, [cat.id, 99999], [], _today(), None,
        )
        p = StudyPlanner.objects.get(user=self.u)
        self.assertEqual(
            set(p.target_categories.values_list('id', flat=True)),
            {cat.id},
        )

    def test_tag_names_are_created_on_the_fly(self):
        StudyPlannerService.update_planner(
            self.u, 10, [], ['brand-new-tag'], _today(), None,
        )
        self.assertTrue(Tag.objects.filter(name='brand-new-tag').exists())
        p = StudyPlanner.objects.get(user=self.u)
        self.assertEqual(
            set(p.target_tags.values_list('name', flat=True)),
            {'brand-new-tag'},
        )

    def test_duplicate_tag_names_are_deduplicated(self):
        StudyPlannerService.update_planner(
            self.u, 10, [], ['tag-a', 'tag-a', ' tag-a '], _today(), None,
        )
        p = StudyPlanner.objects.get(user=self.u)
        self.assertEqual(p.target_tags.count(), 1)


class RecordDailyProgressTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.u = make_user()

    def test_first_call_creates_planner_and_day_row(self):
        answered = StudyPlannerService.record_daily_progress(self.u)
        self.assertEqual(answered, 0)
        self.assertTrue(
            StudyPlanner.objects.filter(user=self.u).exists()
        )
        today = timezone.localdate()
        day = StudyPlannerDay.objects.get(planner__user=self.u, date=today)
        self.assertEqual(day.questions_answered, 0)

    def test_counts_todays_sessions_only(self):
        make_test_history(self.u, total_questions=7)
        make_test_history(self.u, total_questions=3)
        TestHistory.objects.create(
            user=self.u,
            mode='study',
            total_questions=100,
            correct_count=50,
            accuracy=50.0,
            time_spent=100,
            completed_at=timezone.now() - timedelta(days=2),
        )
        answered = StudyPlannerService.record_daily_progress(self.u)
        self.assertEqual(answered, 10)

    def test_second_call_upserts_not_inserts(self):
        make_test_history(self.u, total_questions=5)
        StudyPlannerService.record_daily_progress(self.u)
        make_test_history(self.u, total_questions=8)
        StudyPlannerService.record_daily_progress(self.u)

        today = timezone.localdate()
        rows = StudyPlannerDay.objects.filter(
            planner__user=self.u, date=today,
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().questions_answered, 13)


class DeletePlannerTests(CacheClearingTestCase):
    def test_cascade_removes_day_rows(self):
        u = make_user()
        StudyPlannerService.record_daily_progress(u)
        self.assertGreater(StudyPlannerDay.objects.count(), 0)

        StudyPlannerService.delete_planner(u)
        self.assertEqual(
            StudyPlanner.objects.filter(user=u).count(), 0,
        )
        self.assertEqual(StudyPlannerDay.objects.count(), 0)

    def test_delete_when_none_exists_is_a_noop(self):
        u = make_user()
        StudyPlannerService.delete_planner(u)