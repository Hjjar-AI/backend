# tests/api/test_planning_api.py
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from apps.planning.models import StudyPlanner, StudyPlannerDay
from apps.questions.models import Tag
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_category, make_test_history


class GetPlannerAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_get_creates_planner_on_first_visit(self):
        resp = self.client.get('/api/v1/study-planner/')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(StudyPlanner.objects.filter(user=self.u).exists())

    def test_get_returns_empty_state(self):
        resp = self.client.get('/api/v1/study-planner/')
        data = resp.json()['data']
        self.assertEqual(data['target_questions_per_day'], 10)
        self.assertEqual(data['target_categories'], [])
        self.assertEqual(data['target_tags'], [])
        self.assertEqual(data['daily_progress'], {})
        self.assertIn('today', data)

    def test_daily_progress_returns_iso_keyed_dict(self):
        make_test_history(self.u, total_questions=5)
        # Trigger the day row via record.
        self.client.post('/api/v1/study-planner/progress/')
        resp = self.client.get('/api/v1/study-planner/')
        progress = resp.json()['data']['daily_progress']
        today = timezone.localdate().isoformat()
        self.assertIn(today, progress)
        self.assertEqual(progress[today], 5)


class UpdatePlannerAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_update_with_categories_and_tags(self):
        cat = make_category('target-cat')
        resp = self.client.post(
            '/api/v1/study-planner/update/',
            {
                'target_questions_per_day': 25,
                'target_categories': [cat.id],
                'target_tags': ['focus-tag'],
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        p = StudyPlanner.objects.get(user=self.u)
        self.assertEqual(p.target_questions_per_day, 25)
        self.assertEqual(p.target_categories.count(), 1)
        self.assertTrue(Tag.objects.filter(name='focus-tag').exists())

    def test_target_out_of_range_rejected(self):
        for bad in (0, 1001, -5):
            resp = self.client.post(
                '/api/v1/study-planner/update/',
                {'target_questions_per_day': bad},
                format='json',
            )
            self.assertEqual(resp.status_code, 400)

    def test_categories_accepted_as_csv_string(self):
        cat_a = make_category('cat-a')
        cat_b = make_category('cat-b')
        resp = self.client.post(
            '/api/v1/study-planner/update/',
            {
                'target_questions_per_day': 10,
                'target_categories': f'{cat_a.id},{cat_b.id}',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        p = StudyPlanner.objects.get(user=self.u)
        self.assertEqual(p.target_categories.count(), 2)

    def test_invalid_date_rejected(self):
        resp = self.client.post(
            '/api/v1/study-planner/update/',
            {
                'target_questions_per_day': 10,
                'start_date': 'not-a-date',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_full_replacement_of_categories(self):
        cat_a = make_category('cat-a')
        cat_b = make_category('cat-b')
        self.client.post(
            '/api/v1/study-planner/update/',
            {
                'target_questions_per_day': 10,
                'target_categories': [cat_a.id, cat_b.id],
            },
            format='json',
        )
        self.client.post(
            '/api/v1/study-planner/update/',
            {
                'target_questions_per_day': 10,
                'target_categories': [cat_a.id],
            },
            format='json',
        )
        p = StudyPlanner.objects.get(user=self.u)
        self.assertEqual(
            set(p.target_categories.values_list('id', flat=True)),
            {cat_a.id},
        )


class RecordProgressAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_record_progress_creates_day_row(self):
        """
        The endpoint must create a StudyPlannerDay row carrying the
        count of questions answered today. The previous version of
        this test asserted only the HTTP status — a view that
        returned 200 without writing anything would have passed.
        """
        make_test_history(self.u, total_questions=8)
        resp = self.client.post('/api/v1/study-planner/progress/')
        self.assertEqual(resp.status_code, 200)

        today = timezone.localdate()
        day = StudyPlannerDay.objects.get(
            planner__user=self.u, date=today,
        )
        self.assertEqual(day.questions_answered, 8)

    def test_record_progress_upserts_not_inserts(self):
        """
        Two calls on the same day must touch the same row, not
        create a second one. The service's fast path does an
        UPDATE on the indexed (planner, date) pair when the row
        already exists.
        """
        make_test_history(self.u, total_questions=3)
        self.client.post('/api/v1/study-planner/progress/')
        make_test_history(self.u, total_questions=4)
        self.client.post('/api/v1/study-planner/progress/')

        today = timezone.localdate()
        rows = StudyPlannerDay.objects.filter(
            planner__user=self.u, date=today,
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().questions_answered, 7)

    def test_requires_auth(self):
        self.client.logout()
        resp = self.client.post('/api/v1/study-planner/progress/')
        self.assertIn(resp.status_code, (401, 403))


class DeletePlannerAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_delete_removes_planner(self):
        self.client.get('/api/v1/study-planner/')
        self.assertTrue(StudyPlanner.objects.filter(user=self.u).exists())
        resp = self.client.delete('/api/v1/study-planner/delete/')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(StudyPlanner.objects.filter(user=self.u).exists())


class MyStreakAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_returns_streak_fields(self):
        self.u.current_streak = 3
        self.u.longest_streak = 10
        self.u.save()
        resp = self.client.get('/api/v1/study/streak/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(data['current_streak'], 3)
        self.assertEqual(data['longest_streak'], 10)

    def test_last_study_date_none_when_never_studied(self):
        resp = self.client.get('/api/v1/study/streak/')
        self.assertIsNone(resp.json()['data']['last_study_date'])


class ActivityHeatmapAPITests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.u = make_user('alice')
        self.client.force_login(self.u)

    def test_default_days_is_365(self):
        resp = self.client.get('/api/v1/study/activity-heatmap/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()['data']['days']), 365)

    def test_days_param_clamped(self):
        resp = self.client.get('/api/v1/study/activity-heatmap/?days=9999')
        self.assertEqual(len(resp.json()['data']['days']), 730)
        resp = self.client.get('/api/v1/study/activity-heatmap/?days=0')
        self.assertEqual(len(resp.json()['data']['days']), 7)

    def test_counts_reflected_in_days(self):
        make_test_history(self.u, total_questions=5)
        resp = self.client.get('/api/v1/study/activity-heatmap/?days=7')
        days = resp.json()['data']['days']
        today = timezone.localdate().isoformat()
        today_entry = next(d for d in days if d['date'] == today)
        self.assertEqual(today_entry['count'], 5)

    def test_totals_and_streaks_present(self):
        resp = self.client.get('/api/v1/study/activity-heatmap/?days=7')
        data = resp.json()['data']
        for key in ('total_questions', 'active_days', 'max_daily',
                    'current_streak', 'longest_streak'):
            self.assertIn(key, data)

    def test_days_are_contiguous(self):
        from datetime import date
        resp = self.client.get('/api/v1/study/activity-heatmap/?days=14')
        days = resp.json()['data']['days']
        parsed = [date.fromisoformat(d['date']) for d in days]
        for a, b in zip(parsed, parsed[1:]):
            self.assertEqual((b - a).days, 1)