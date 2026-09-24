# backend/apps/planning/views.py

import logging
from datetime import datetime, timedelta

from django.db.models import Prefetch
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from .models import StudyPlanner, StudyPlannerDay
from .serializers import StudyPlannerSerializer
from .services import StudyPlannerService
from apps.core.utils import api_success, api_error, safe_int
from apps.exams.services.activity import daily_activity

logger = logging.getLogger(__name__)


class GetPlannerView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        cutoff = timezone.localdate() - timedelta(days=90)
        planner, _ = (
            StudyPlanner.objects
            .prefetch_related(
                Prefetch(
                    'days',
                    queryset=StudyPlannerDay.objects.filter(date__gte=cutoff),
                    to_attr='recent_days_prefetched',
                )
            )
            .get_or_create(user=request.user)
        )
        serializer = StudyPlannerSerializer(planner)
        return api_success(data=serializer.data)


class UpdatePlannerView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data
        try:
            target = int(data.get('target_questions_per_day', 10))

            raw_categories = data.get('target_categories', [])
            raw_tags = data.get('target_tags', [])

            start_date_raw = data.get('start_date')
            end_date_raw = data.get('end_date')

            start_date = (
                datetime.strptime(start_date_raw, '%Y-%m-%d').date()
                if start_date_raw else timezone.localdate()
            )
            end_date = (
                datetime.strptime(end_date_raw, '%Y-%m-%d').date()
                if end_date_raw else None
            )
        except (ValueError, TypeError) as e:
            logger.warning(
                'Invalid study planner update payload (user_id=%s): %s',
                request.user.id,
                type(e).__name__,
            )
            return api_error('خطأ في بيانات الإدخال', 400)

        if not (1 <= target <= 1000):
            return api_error('الهدف اليومي يجب أن يكون بين 1 و 1000 سؤال', 400)
        if end_date is not None and end_date < start_date:
            return api_error('تاريخ النهاية يجب ألا يسبق تاريخ البداية', 400)

        category_ids = []
        if isinstance(raw_categories, list):
            for c in raw_categories:
                try:
                    category_ids.append(int(c))
                except (TypeError, ValueError):
                    continue
        elif isinstance(raw_categories, str):
            for piece in raw_categories.split(','):
                piece = piece.strip()
                if not piece:
                    continue
                try:
                    category_ids.append(int(piece))
                except (TypeError, ValueError):
                    continue

        tag_names = []
        if isinstance(raw_tags, list):
            for t in raw_tags:
                if isinstance(t, str) and t.strip():
                    tag_names.append(t.strip())
        elif isinstance(raw_tags, str):
            for piece in raw_tags.split(','):
                piece = piece.strip()
                if piece:
                    tag_names.append(piece)

        StudyPlannerService.update_planner(
            request.user, target, category_ids, tag_names, start_date, end_date,
        )
        return api_success(message='تم تحديث الخطة')


class RecordProgressView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        StudyPlannerService.record_daily_progress(request.user)
        return api_success(message='تم تسجيل التقدم')


class DeletePlannerView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        StudyPlannerService.delete_planner(request.user)
        return api_success(message='تم حذف الخطة')


class MyStreakView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        u = request.user
        return api_success(data={
            'current_streak': u.current_streak or 0,
            'longest_streak': u.longest_streak or 0,
            'last_study_date': u.last_study_date.isoformat() if u.last_study_date else None,
        })


class ActivityHeatmapView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        days = safe_int(
            request.query_params.get('days'), 365,
            minimum=7, maximum=730,
        )

        now = timezone.now()
        cutoff = now - timedelta(days=days - 1)

        # Includes completed regular sessions and master exams.
        count_by_date = {
            date: row['questions']
            for date, row in daily_activity(request.user.id, cutoff.date()).items()
        }

        start_date = cutoff.date()
        end_date = now.date()
        day_list = []
        current = start_date
        one_day = timedelta(days=1)
        while current <= end_date:
            iso = current.isoformat()
            day_list.append({
                'date': iso,
                'count': count_by_date.get(iso, 0),
            })
            current += one_day

        total_questions = sum(d['count'] for d in day_list)
        active_days = sum(1 for d in day_list if d['count'] > 0)
        max_daily = max((d['count'] for d in day_list), default=0)

        user = request.user

        return api_success(data={
            'days': day_list,
            'total_questions': total_questions,
            'active_days': active_days,
            'max_daily': max_daily,
            'current_streak': user.current_streak or 0,
            'longest_streak': user.longest_streak or 0,
        })
