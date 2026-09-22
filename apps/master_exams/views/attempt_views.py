# backend/apps/master_exams/views/attempt_views.py
"""
Attempt runner for master exams.

Authorization on these views is *participation*, not capability:
the caller must be in the exam's audience (or be an author / hold
'manage_any' as a bypass). The capability system gates creation
and management; participation is object-level and lives in
MasterExamService.user_can_access.
"""
import logging

from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.utils import api_success, api_error
from apps.core.permissions import IsMasterExamParticipant
from apps.core.throttles import (
    MasterExamAnswerRateThrottle,
    MasterExamStartRateThrottle,
)
from apps.feedback.services import FeedbackService

from ..models import MasterExam, MasterExamAttempt
from ..serializers import (
    MasterExamAttemptSerializer,
    MasterExamAttemptStartSerializer,
    MasterExamSubmitAnswerSerializer,
    MasterExamGotoSerializer,
    MasterExamFlagSerializer,
)
from ..services import (
    MasterExamService,
    MasterExamAttemptService,
    MasterExamSweeper,
)
from .common import _can_manage_exam
from ._error_map import exam_error_response

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════
# Shared helper — caller's latest attempt
# ═════════════════════════════════════════════════════════════════════
#
# Every view in this module opened with the same lookup: find the
# caller's most recent attempt on this exam. Hoisting it keeps the
# filter and the ordering in one place, so a future change (scoping
# to a specific attempt id, adding select_related, adding a status
# pre-filter) lands once instead of six times. The ordering and
# filter semantics are unchanged.

def _latest_attempt_for(exam, user):
    """
    Return the caller's most recent attempt for `exam`, or None if
    the caller has never started one. Ordered by descending
    `started_at`, so the result is deterministic when a make-up
    attempt exists alongside the original.
    """
    return (
        MasterExamAttempt.objects
        .filter(master_exam=exam, user=user)
        .order_by('-started_at')
        .first()
    )


# ── Error translation ────────────────────────────────────────────────
#
# All service error codes route through the shared map in
# `._error_map`. The previous revision had its own local
# `_attempt_error_details` table here that duplicated the same shape
# for the attempt-flow codes; it now delegates to the shared map so
# a code added in one place is recognised everywhere. The wire
# contract is unchanged: the localized message is the response
# `message`, and the raw code is forwarded in `details['code']` when
# it is a recognised sentinel. The frontend's
# `masterExamAttemptStore` still detects `details.code ==
# 'TIME_EXPIRED'` and routes the user to the results page.


class MasterExamStartAttemptView(APIView):
    """
    Start (or resume) an attempt.

    Preview mode is gated by the additional 'master_exams.preview'
    capability plus the ability to manage the specific exam.
    """
    permission_classes = [IsMasterExamParticipant]
    throttle_classes = [MasterExamStartRateThrottle]

    def post(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)

        if not MasterExamService.user_can_access(request.user, exam):
            return api_error('غير مصرح لك', 403)

        serializer = MasterExamAttemptStartSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        is_preview = serializer.validated_data['preview']

        if is_preview:
            if not request.user.has_capability('master_exams.preview'):
                return api_error('المعاينة متاحة للمشرفين فقط', 403)
            if not _can_manage_exam(request.user, exam):
                return api_error('غير مصرح لك', 403)

        try:
            attempt = MasterExamAttemptService.start(
                request.user, exam, is_preview=is_preview,
            )
        except ValueError as e:
            msg, status, details = exam_error_response(str(e))
            return api_error(msg, status, details=details)

        # Preview sessions return a dict payload rather than a
        # persisted attempt.
        if isinstance(attempt, dict):
            return api_success(data={
                'session_id': attempt['session_id'],
                'master_exam_id': exam.id,
                'exam_name': attempt['exam_name_snapshot'],
                'is_preview': True,
                'question_ids': attempt['question_ids'],
                'questions': attempt.get('questions') or [],
                'answers': attempt['answers'],
                'current_question_id': attempt['current_question_id'],
                'started_at': attempt['started_at'].isoformat(),
                'deadline_at': None,
                'is_makeup': False,
                'duration_minutes': attempt.get('duration_minutes'),
                'grace_seconds': attempt.get('grace_seconds', 180),
            })

        return api_success(
            data=MasterExamAttemptSerializer(attempt).data,
            message='بدأ الامتحان',
        )


class MasterExamAttemptStatusView(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        attempt = _latest_attempt_for(exam, request.user)
        if not attempt:
            return api_error('لا توجد محاولة', 404)

        # Single-attempt sweep. If the deadline plus grace has passed,
        # this force-finishes the attempt before we read its state.
        # The refresh_from_db() is only needed when the sweep actually
        # wrote to the row.
        if MasterExamSweeper.sweep_attempt(attempt):
            attempt.refresh_from_db()

        return api_success(data=MasterExamAttemptService.status(attempt))


class MasterExamCurrentQuestionView(APIView):
    """
    Fetch the current question for the caller's attempt.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        attempt = _latest_attempt_for(exam, request.user)
        if not attempt:
            return api_error('لا توجد محاولة', 404)
        if attempt.is_complete:
            return api_error('المحاولة منتهية', 400)

        payload = MasterExamAttemptService.current_question(attempt)
        if payload is None:
            return api_error('لا يوجد سؤال', 404)
        return api_success(data=payload)


class MasterExamSubmitAnswerView(APIView):
    """
    Save one answer against the caller's attempt.

    On TIME_EXPIRED the response is a 400 with the localized message
    AND `details.code = 'TIME_EXPIRED'`. The frontend store detects
    that code and marks the attempt complete, so the runner
    immediately routes to the results page instead of leaving the
    user on a question whose every subsequent save returns the same
    400.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [MasterExamAnswerRateThrottle]

    def post(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        attempt = _latest_attempt_for(exam, request.user)
        if not attempt:
            return api_error('لا توجد محاولة', 404)
        if attempt.is_complete:
            return api_error('المحاولة منتهية', 400)

        serializer = MasterExamSubmitAnswerSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        try:
            attempt = MasterExamAttemptService.submit_answer(
                attempt,
                serializer.validated_data['question_id'],
                serializer.validated_data['answer'],
                serializer.validated_data['confidence'],
                serializer.validated_data.get('error_reason'),
            )
        except ValueError as e:
            msg, status, details = exam_error_response(str(e))
            return api_error(msg, status, details=details)

        return api_success(data={
            'success': True,
            'current_question_id': attempt.current_question_id,
        })


class MasterExamGotoView(APIView):
    """
    Jump to a specific question.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        attempt = _latest_attempt_for(exam, request.user)
        if not attempt or attempt.is_complete:
            return api_error('لا توجد محاولة نشطة', 404)

        serializer = MasterExamGotoSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        try:
            attempt = MasterExamAttemptService.goto_question(
                attempt, serializer.validated_data['question_id'],
            )
        except ValueError as e:
            msg, status, details = exam_error_response(str(e))
            return api_error(msg, status, details=details)

        return api_success(data={
            'current_question_id': attempt.current_question_id,
        })


class MasterExamFinishAttemptView(APIView):
    """
    Finish the caller's attempt: grade, compute weighted score,
    persist. Idempotent.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        attempt = _latest_attempt_for(exam, request.user)
        if not attempt:
            return api_error('لا توجد محاولة', 404)

        try:
            attempt = MasterExamAttemptService.finish(attempt, forced=False)
        except Exception:
            logger.exception(
                'Failed to finish master exam attempt %s', attempt.id,
            )
            return api_error('فشل إنهاء الامتحان', 500)

        return api_success(
            data=MasterExamAttemptSerializer(attempt).data,
            message='تم إنهاء الامتحان',
        )


class MasterExamFlagView(APIView):
    """
    Flag a question from within an exam attempt. The flag row is
    linked to the attempt so the moderator dashboard can show
    "raised during exam X".
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        attempt = _latest_attempt_for(exam, request.user)
        if not attempt:
            return api_error('لا توجد محاولة', 404)

        serializer = MasterExamFlagSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        question_id = serializer.validated_data['question_id']
        in_exam = exam.exam_questions.filter(question_id=question_id).exists()
        if not in_exam:
            return api_error('السؤال ليس جزءاً من هذا الامتحان', 400)

        success = FeedbackService.flag_question(
            request.user.id,
            question_id,
            serializer.validated_data.get('reason'),
            master_exam_attempt=attempt,
        )
        if not success:
            return api_error('لقد قمت بالإبلاغ عن هذا السؤال بالفعل', 400)

        return api_success(message='تم الإبلاغ — سيصل إلى المشرف فوراً')