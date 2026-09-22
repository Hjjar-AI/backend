# backend/apps/master_exams/views/results_views.py
"""
Results dashboard and CSV exports for master exams.

Three authorization tiers, all exercised here:
  • 'view_results_any'      — view any exam's results
  • 'view_results_own'      — view results of exams you authored
  • 'manage_own' / 'manage_any' — implied: managing an exam implies
                                  viewing its results
"""
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.utils import api_success, api_error

from ..models import MasterExam
from ..serializers import MasterExamDetailSerializer
from ..services import MasterExamSweeper, MasterExamResultsService
from .common import _can_view_results


# The sweep gate interval. Prevents the results dashboard from
# running a global expired-attempt scan more than once per interval
# across results dashboards.
_SWEEP_GATE_SECONDS = 30


class MasterExamResultsView(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        if not _can_view_results(request.user, exam):
            return api_error('غير مصرح لك', 403)

        # Cache-gated global sweep. Every request after the first
        # within the gate window skips the query. The gate uses
        # Django's configured cache backend, so a multi-process
        # deployment (Memcached/Redis) shares the gate across workers.
        sweep_key = 'master_exam_sweep_gate'
        if cache.add(sweep_key, True, _SWEEP_GATE_SECONDS):
            try:
                MasterExamSweeper.sweep_expired(limit=50)
            except Exception:
                cache.delete(sweep_key)
                raise

        snapshot = MasterExamResultsService.dashboard_snapshot(exam)
        data = {
            'exam': MasterExamDetailSerializer(
                exam, context={'request': request},
            ).data,
            'summary': MasterExamResultsService.live_summary(exam),
            'per_user': MasterExamResultsService.per_user_rows(exam),
            'per_question': snapshot['per_question'],
            'per_category': snapshot['per_category'],
            'per_difficulty': snapshot['per_difficulty'],
            'histogram': MasterExamResultsService.score_histogram(exam),
            'flags': MasterExamResultsService.flags_raised_during_exam(exam),
        }
        return api_success(data=data)


class MasterExamResultsSummaryCSVView(APIView):
    """
    Per-user summary CSV. Same authorization as the JSON dashboard.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        if not _can_view_results(request.user, exam):
            return api_error('غير مصرح لك', 403)

        csv_body = MasterExamResultsService.csv_summary(exam)
        filename = f'master_exam_{exam.id}_summary.csv'

        response = HttpResponse(csv_body, content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class MasterExamResultsMatrixCSVView(APIView):
    """
    Question-by-user correctness matrix CSV. Same authorization as
    the JSON dashboard.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        if not _can_view_results(request.user, exam):
            return api_error('غير مصرح لك', 403)

        csv_body = MasterExamResultsService.csv_matrix(exam)
        filename = f'master_exam_{exam.id}_matrix.csv'

        response = HttpResponse(csv_body, content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
