# backend/apps/master_exams/views/lifecycle_views.py
"""
Lifecycle status transitions for master exams: publish, cancel,
publish-to-bank.
"""
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.utils import api_success, api_error

from ..models import MasterExam
from ..serializers import MasterExamDetailSerializer
from ..services import MasterExamService
from .common import _can_manage_exam
from ._error_map import exam_error_response


class MasterExamPublishView(APIView):
    """
    Transition draft → published. Requires 'master_exams.manage_own'
    plus authorship, or 'manage_any'. Rejects a second publish and
    rejects publishing an exam with no questions.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        if not _can_manage_exam(request.user, exam):
            return api_error('غير مصرح لك', 403)

        try:
            exam = MasterExamService.publish(exam, request=request)
        except ValueError as e:
            msg, status, details = exam_error_response(str(e))
            return api_error(msg, status, details=details)

        return api_success(
            data=MasterExamDetailSerializer(exam, context={'request': request}).data,
            message='تم نشر الامتحان',
        )


class MasterExamCancelView(APIView):
    """
    Transition any non-terminal status → cancelled. Requires
    'master_exams.manage_own' plus authorship, or 'manage_any'.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        if not _can_manage_exam(request.user, exam):
            return api_error('غير مصرح لك', 403)

        try:
            exam = MasterExamService.cancel(exam, request=request)
        except ValueError as e:
            msg, status, details = exam_error_response(str(e))
            return api_error(msg, status, details=details)

        return api_success(
            data=MasterExamDetailSerializer(exam, context={'request': request}).data,
            message='تم إلغاء الامتحان',
        )


class MasterExamPublishToBankView(APIView):
    """
    Transition completed → published_to_bank. This is a two-gate
    action:

      1. The caller must be able to manage the exam (author, or
         'manage_any').
      2. The caller must additionally hold 'master_exams.publish_to_bank'.

    The second gate exists because publishing to the bank is the
    point at which draft questions become permanent bank content and
    the questions are tagged by exam date. A deployment may want to
    require a second pair of eyes on that transition without
    withholding every other management action.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        if not _can_manage_exam(request.user, exam):
            return api_error('غير مصرح لك', 403)
        if not request.user.has_capability('master_exams.publish_to_bank'):
            return api_error('غير مصرح لك بنشر الامتحان في البنك', 403)

        try:
            exam = MasterExamService.publish_to_bank(exam, request=request)
        except ValueError as e:
            msg, status, details = exam_error_response(str(e))
            return api_error(msg, status, details=details)

        return api_success(
            data=MasterExamDetailSerializer(exam, context={'request': request}).data,
            message='تم نشر الأسئلة في البنك',
        )