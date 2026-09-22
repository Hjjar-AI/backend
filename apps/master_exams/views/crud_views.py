# backend/apps/master_exams/views/crud_views.py
"""
CRUD endpoints for master exams.
"""
from django.db.models import Count, Q, Prefetch
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.utils import api_success, api_error

from ..models import MasterExam, MasterExamAttempt, MasterExamAcknowledgement
from ..serializers import (
    MasterExamListSerializer,
    MasterExamDetailSerializer,
    MasterExamCreateSerializer,
    MasterExamUpdateSerializer,
    MasterExamDeleteSerializer,
    MasterExamAttemptSerializer,
)
from ..services import MasterExamService
from .common import _is_author, _can_manage_exam
from ._error_map import exam_error_response


def _annotate_list_queryset(qs):
    """
    Annotate the counters the list serializer reads. Shared between
    the plain list view and the needs-acknowledgement view so both
    stay in lockstep when a new counter is added.
    """
    return qs.annotate(
        _exam_question_count=Count('exam_questions', distinct=True),
        _co_attending_count=Count('co_attendings', distinct=True),
        _audience_user_count=Count('audience_users', distinct=True),
        _audience_group_count=Count('audience_groups', distinct=True),
    )


def _prefetch_my_attempts(user):

    return Prefetch(
        'attempts',
        queryset=MasterExamAttempt.objects
            .filter(user=user)
            .select_related('master_exam')
            .order_by('-started_at'),
        to_attr='_my_attempts_prefetched',
    )


class MasterExamListCreateView(APIView):
    """
    GET  — exams the caller owns, co-attends, or is in the audience
           of, plus (for admins) every exam.
    POST — create a new master exam. Requires 'master_exams.create'.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if user.has_capability('master_exams.manage_any'):
            qs = MasterExam.objects.all()
        else:
            owned = Q(primary_attending=user) | Q(co_attendings=user)
            assigned_ids = MasterExamService._assigned_exam_ids(user)
            qs = MasterExam.objects.filter(
                owned | Q(id__in=assigned_ids),
            ).distinct()

        now = timezone.now()

        qs = (
            qs.select_related('primary_attending')
            .prefetch_related('co_attendings')
        )
        qs = _annotate_list_queryset(qs)
        qs = qs.prefetch_related(_prefetch_my_attempts(user))

        status_filter = request.query_params.get('status')
        if status_filter == 'active':
            qs = qs.filter(opens_at__lte=now, closes_at__gt=now)
        elif status_filter == 'upcoming':
            qs = qs.filter(opens_at__gt=now)
        elif status_filter == 'past':
            qs = qs.filter(closes_at__lte=now)

        qs = qs.order_by('-opens_at')
        serializer = MasterExamListSerializer(
            qs, many=True, context={'request': request},
        )
        return api_success(data={
            'items': serializer.data,
            'total': qs.count(),
        })

    def post(self, request):
        if not request.user.has_capability('master_exams.create'):
            return api_error('غير مصرح لك بإنشاء امتحان رئيسي', 403)

        serializer = MasterExamCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        try:
            exam = MasterExamService.create(
                primary_attending=request.user,
                data=serializer.validated_data,
                request=request,
            )
        except ValueError as e:
            msg, status, details = exam_error_response(str(e))
            return api_error(msg, status, details=details)

        return api_success(
            data=MasterExamDetailSerializer(exam, context={'request': request}).data,
            message='تم إنشاء الامتحان الرئيسي',
            code=201,
        )


class MasterExamDetailView(APIView):
    """
    GET    — detail.
    PUT    — update.
    DELETE — delete.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        exam = get_object_or_404(
            MasterExam.objects
            .prefetch_related(
                'co_attendings',
                'audience_users',
                'audience_groups',
            )
            .annotate(
                _exam_question_count=Count('exam_questions', distinct=True),
            ),
            pk=pk,
        )
        if not MasterExamService.user_can_access(request.user, exam):
            return api_error('غير مصرح لك', 403)

        attempt = MasterExamAttempt.objects.filter(
            master_exam=exam, user=request.user,
        ).first()
        data = MasterExamDetailSerializer(exam, context={'request': request}).data
        data['my_attempt'] = (
            MasterExamAttemptSerializer(attempt).data if attempt else None
        )
        data['has_acknowledged'] = MasterExamAcknowledgement.objects.filter(
            user=request.user, master_exam=exam,
        ).exists()
        return api_success(data=data)

    def put(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        if not _can_manage_exam(request.user, exam):
            return api_error('غير مصرح لك بتعديل هذا الامتحان', 403)

        serializer = MasterExamUpdateSerializer(
            data=request.data,
            context={'exam': exam},
        )
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        data = dict(serializer.validated_data)
        expected_version = data.pop('expected_version')
        data = {k: v for k, v in data.items() if k in request.data}

        try:
            exam = MasterExamService.update(
                exam, data, expected_version=expected_version, request=request,
            )
        except ValueError as e:
            msg, status, details = exam_error_response(str(e))
            return api_error(msg, status, details=details)

        return api_success(
            data=MasterExamDetailSerializer(exam, context={'request': request}).data
        )

    def delete(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        if not _can_manage_exam(request.user, exam):
            return api_error('غير مصرح لك بحذف هذا الامتحان', 403)

        serializer = MasterExamDeleteSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error(
                'يجب تحديد طريقة التعامل مع المسودات', 400,
                details=serializer.errors,
            )

        try:
            MasterExamService.delete(
                exam,
                serializer.validated_data['delete_mode'],
                request=request,
            )
        except ValueError as e:
            msg, status, details = exam_error_response(str(e))
            return api_error(msg, status, details=details)

        return api_success(message='تم حذف الامتحان')


class MasterExamAcknowledgeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        if not MasterExamService.user_can_access(request.user, exam):
            return api_error('غير مصرح لك', 403)
        MasterExamService.acknowledge(exam, request.user)
        return api_success(message='تم')


class MasterExamNeedsAcknowledgementView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = MasterExamService.needs_acknowledgement(request.user)
        qs = _annotate_list_queryset(qs).prefetch_related('co_attendings')
        qs = qs.prefetch_related(_prefetch_my_attempts(request.user))
        serializer = MasterExamListSerializer(
            qs, many=True, context={'request': request},
        )
        return api_success(data={
            'count': qs.count(),
            'items': serializer.data,
        })