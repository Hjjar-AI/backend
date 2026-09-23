# backend/apps/exams/views/session_views.py
"""
Plain (non-master-exam) session runner.

Start, per-question navigation, answer submission, pause/resume,
discard, status, and finish. Every view here operates on the
caller's own ``ExamSession`` row; cross-user access is impossible
because every lookup filters on ``user=request.user``.

CAPABILITY GATE ON StartSessionView
-----------------------------------
``tests.start`` is now enforced on session creation. It is granted
to every default role, so no user is affected by the change in the
default deployment — but a deployment that wants to restrict
who can begin a test can do so by unchecking the capability on a
role in the permissions panel. The downstream views
(``GetQuestionView``, ``SubmitAnswerView``, etc.) continue to gate
on ``IsAuthenticated`` and the session's own ownership: once a
session exists, the person who created it can finish it, regardless
of any later capability change.

Master exams have their own authorization path
(``MasterExamStartAttemptView`` + ``MasterExamService.user_can_access``)
and are not gated by ``tests.start``.
"""

from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from ._helpers import _exam_duration_minutes
from ..models import ExamSession, Blueprint
from ..serializers import (
    ExamSessionSerializer,
    SessionIdSerializer,
    SessionIdOrModeSerializer,
    SubmitAnswerSerializer,
)
from ..services import ExamService, BlueprintService
from apps.core.permissions import HasCapability
from apps.core.utils import (
    api_success,
    api_error,
    dedupe_ordered,
    safe_int,
)
from apps.questions.models import Question
from apps.questions.services import QuestionService
from apps.learning.srs_service import SRSService


def _session_progress_payload(session):
    data = {
        'session_id': session.session_id,
        'question_ids': session.question_ids,
        'current_index': session.current_index,
        'answers': session.answers,
        'tag': session.tag,
        'started_at': session.started_at.isoformat() if session.started_at else None,
        'accumulated_time': session.accumulated_time,
        'is_active': session.is_active,
    }
    if session.mode in ('exam', 'study', 'recall'):
        data['duration_minutes'] = _exam_duration_minutes()
    return data


class StartSessionView(APIView):
    permission_classes = [HasCapability]
    required_capability = 'tests.start'

    def post(self, request, mode):
        valid_modes = {value for value, _ in ExamSession.MODE_CHOICES}
        if mode not in valid_modes:
            return api_error('وضع غير صالح', 400)

        data = request.data
        question_ids = data.get('question_ids')
        blueprint = None
        blueprint_id = data.get('blueprint_id')

        if blueprint_id is not None:
            if not request.user.has_capability('tests.use_blueprint'):
                return api_error('غير مصرح لك باستخدام نماذج الامتحان', 403)
            try:
                blueprint = Blueprint.objects.get(id=int(blueprint_id), is_active=True)
            except (Blueprint.DoesNotExist, TypeError, ValueError):
                return api_error('نموذج الامتحان غير موجود', 404)
            max_quiz = getattr(settings, 'MAX_QUIZ_QUESTIONS', 200)
            count = safe_int(data.get('limit'), 50, minimum=1, maximum=max_quiz)
            assembled = BlueprintService.select_question_ids(blueprint, count)
            if not assembled:
                return api_error('لا توجد أسئلة كافية مطابقة للنموذج', 404)
            question_ids = assembled

        elif data.get('use_srs'):
            max_quiz = getattr(settings, 'MAX_QUIZ_QUESTIONS', 200)
            limit = safe_int(data.get('limit'), 20, minimum=1, maximum=max_quiz)
            due_ids = SRSService.due_question_ids(request.user, limit=limit)
            if not due_ids:
                return api_error('لا توجد أسئلة مستحقة للمراجعة حالياً', 404)
            question_ids = due_ids

        elif question_ids is not None:
            max_quiz = getattr(settings, 'MAX_QUIZ_QUESTIONS', 200)
            if not isinstance(question_ids, list):
                return api_error('معرفات الأسئلة غير صالحة', 400)
            if len(question_ids) == 0:
                return api_error('قائمة معرفات الأسئلة لا يمكن أن تكون فارغة', 400)
            if not all(
                isinstance(qid, int) and not isinstance(qid, bool) and qid > 0
                for qid in question_ids
            ):
                return api_error('معرفات الأسئلة غير صالحة', 400)
            if len(question_ids) > max_quiz:
                return api_error(f'الحد الأقصى {max_quiz} سؤال', 400)
            # Order-preserving dedupe — shared with the rating-batch
            # view via `dedupe_ordered`.
            unique_ids = dedupe_ordered(question_ids)
            existing_ids = set(
                Question.objects.visible_to(request.user)
                .filter(id__in=unique_ids)
                .values_list('id', flat=True)
            )
            if len(existing_ids) != len(unique_ids):
                return api_error('بعض معرفات الأسئلة غير موجودة', 400)
            question_ids = unique_ids

        else:
            filters = {}
            tag = data.get('tag')
            use_bookmarks = data.get('use_bookmarks', False)
            verified_only = data.get('verified_only', False)

            # Category selection has two accepted shapes on the wire.
            #
            #   • `category_ids` — an array of ints. This is what the
            #     current StudySetup.vue sends: the multi-select grid
            #     is a checkbox picker, so an array is the natural
            #     shape and there is no reason to serialize it.
            #
            #   • `category` — a single int. Kept for backward
            #     compatibility with an older client that still has
            #     the single-category dropdown. A new client never
            #     sends this.
            #
            # Both paths normalize into `filters['category_ids']` so
            # the queryset builder only has one shape to handle.
            category = data.get('category')                # legacy single
            category_ids = data.get('category_ids')        # new array

            difficulty = data.get('difficulty')
            tags_filter = data.get('tags_filter', '')
            max_quiz = getattr(settings, 'MAX_QUIZ_QUESTIONS', 200)
            limit = safe_int(data.get('limit'), 200, minimum=1, maximum=max_quiz)

            if tag:
                filters['tag'] = tag
            if category_ids:
                if not isinstance(category_ids, list):
                    return api_error('معرفات التصنيفات غير صالحة', 400)
                filters['category_ids'] = category_ids
            elif category:
                filters['category_ids'] = [category]
            if difficulty:
                filters['difficulty'] = difficulty
            if verified_only:
                filters['verified'] = 'yes'
            if use_bookmarks:
                filters['bookmark_user_id'] = request.user.id
            if tags_filter:
                filters['tags_filter'] = [
                    t.strip() for t in tags_filter.split(',') if t.strip()
                ]

            questions = QuestionService.get_questions(
                filters, limit=limit, offset=0, user=None,
            )
            question_ids = list(questions.values_list('id', flat=True))

            if not question_ids:
                return api_error('لا توجد أسئلة متاحة', 404)

        # ── Session label ──────────────────────────────────────────────
        #
        # `session_label` is the preferred key for the string that gets
        # persisted to StudySession.tag / TestHistory.tag and shown in
        # the user's history. It is decoupled from the tag FILTER so a
        # session sourced from bookmarks or SRS can carry a display
        # label like "From bookmarks" or "Smart review" without
        # accidentally filtering by a tag that does not exist.
        #
        # `tag` remains the fallback because the Dashboard's SRS
        # quick-action (startSRS) sends only `tag`. That path is a
        # special case where the label and the filter would have been
        # the same string anyway — the SRS source produces its own
        # question list, so the `tag` key on that payload is purely
        # descriptive and never reaches the filter branch above (it
        # takes the `use_srs` branch).
        #
        # `session_label` is never used as a filter. If a caller sends
        # both keys, the label wins for the display name and `tag`
        # still drives the filter — that is the whole point of the
        # split.
        session_label = data.get('session_label') or data.get('tag') or None

        session = ExamService.start_session(
            request.user, mode, question_ids, session_label, blueprint=blueprint,
        )

        response_data = ExamSessionSerializer(session).data
        if mode in ('exam', 'study', 'recall'):
            response_data['duration_minutes'] = _exam_duration_minutes()

        return api_success(data=response_data, message='تم بدء الجلسة', code=200)


class GetQuestionView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        session_id = request.query_params.get('session_id')
        if not session_id:
            return api_error('معرف الجلسة مطلوب', 400)

        session = get_object_or_404(
            ExamSession, session_id=session_id, user=request.user,
        )
        result = ExamService.get_question(session, session.current_index)

        if result is None:
            return api_error('السؤال غير موجود', 404)

        return api_success(data=result, code=200)


class SubmitAnswerView(APIView):
    """
    Record one answer, optionally advancing the current index.

    SESSION INDEX CONTRACT (this revision)
    --------------------------------------
    The submitted answer is written against whatever index the
    LOCKED row holds at the moment `ExamService.submit_answer`
    acquires the row lock. The study-mode feedback lookup uses the
    same locked-row question id, so the feedback always describes
    the question the answer was actually recorded against.

    The previous implementation computed the feedback question id
    from the caller's own pre-lock `session.current_index`. In
    sequential use the two agreed; under concurrent submissions on
    the same session (two tabs, network retry, browser restore from
    a second device) they could diverge, and the caller would
    receive feedback for a question other than the one it just
    answered. The service now returns the answered question id
    alongside the session — see `AnswerSubmission` — and this view
    reads it from there.

    Response data contains `{new_index, explanation, is_correct}`.
    When the submission was not an answer (action was
    `previous` / `goto`, or the answer was `None`), `explanation`
    and `is_correct` are both `None`, matching the previous
    behaviour for the same inputs.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        body = SubmitAnswerSerializer(data=request.data)
        if not body.is_valid():
            return api_error('معرف الجلسة مطلوب', 400, details=body.errors)

        session = get_object_or_404(
            ExamSession,
            session_id=body.validated_data['session_id'],
            user=request.user,
        )

        try:
            submission = ExamService.submit_answer(
                session,
                body.validated_data['answer'],
                body.validated_data['action'],
                body.validated_data.get('target_index'),
                body.validated_data.get('confidence'),
                body.validated_data.get('error_reason'),
                body.validated_data.get('pre_answer'),
            )
        except ValueError as e:
            return api_error(str(e), 400)

        session = submission.session

        # Study-mode feedback is sourced from the session's frozen
        # grading snapshot so a mid-session edit cannot flip the
        # explanation or the correctness indicator the candidate sees.
        #
        # The `answer is not None` guard is preserved: for a pure
        # navigation (action was `previous` / `goto` with no answer
        # attached) the response carries `explanation: null,
        # `is_correct: null` — same shape the pre-fix version produced
        # for the same inputs.
        #
        # `submission.answered_qid` is read from the service rather
        # than recomputed here. See the class docstring.
        explanation = None
        is_correct = None
        if (
            session.mode in ('study', 'recall')
            and body.validated_data['answer'] is not None
            and submission.answered_qid is not None
        ):
            explanation, is_correct = ExamService.study_feedback(
                session, submission.answered_qid, body.validated_data['answer'],
            )

        return api_success(data={
            'new_index': session.current_index,
            'explanation': explanation,
            'is_correct': is_correct,
        })


class FinishSessionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        body = SessionIdSerializer(data=request.data)
        if not body.is_valid():
            return api_error('معرف الجلسة مطلوب', 400, details=body.errors)

        session = get_object_or_404(
            ExamSession,
            session_id=body.validated_data['session_id'],
            user=request.user,
        )

        try:
            result = ExamService.finish_session(session, request.user)
        except ValueError as e:
            return api_error(str(e), 400)

        return api_success(data=result, code=200)


class PauseSessionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        body = SessionIdSerializer(data=request.data)
        if not body.is_valid():
            return api_error('معرف الجلسة مطلوب', 400, details=body.errors)

        session = get_object_or_404(
            ExamSession,
            session_id=body.validated_data['session_id'],
            user=request.user,
        )
        session = ExamService.pause_session(session)
        return api_success(data={
            'message': 'تم حفظ التقدم',
            'session_id': session.session_id,
        })


class ResumeSessionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        body = SessionIdOrModeSerializer(data=request.data)
        if not body.is_valid():
            return api_error('بيانات غير صالحة', 400, details=body.errors)

        session_id = body.validated_data.get('session_id')
        mode = body.validated_data.get('mode')

        if session_id:
            session = get_object_or_404(
                ExamSession,
                session_id=session_id,
                user=request.user,
            )
        else:
            session = (
                ExamSession.objects
                .filter(user=request.user, mode=mode, is_active=False)
                .order_by('-created_at')
                .first()
            )

        if not session:
            return api_error('لا توجد جلسة محفوظة', 404)

        session = ExamService.resume_session(session)

        data = _session_progress_payload(session)
        data['message'] = 'تم استئناف الجلسة'

        return api_success(data=data)


class DiscardProgressView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        body = SessionIdOrModeSerializer(data=request.data)
        if not body.is_valid():
            return api_error('بيانات غير صالحة', 400, details=body.errors)

        session_id = body.validated_data.get('session_id')
        mode = body.validated_data.get('mode')

        if session_id:
            sessions = ExamSession.objects.filter(
                session_id=session_id, user=request.user,
            )
        elif mode:
            sessions = ExamSession.objects.filter(user=request.user, mode=mode)
        else:
            return api_error('معرف الجلسة أو الوضع مطلوب', 400)

        count = sessions.count()
        sessions.delete()
        return api_success(data={
            'message': 'تم حذف التقدم المحفوظ',
            'deleted_count': count,
        })


class StatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        session_id = request.query_params.get('session_id')
        mode = request.query_params.get('mode')

        if session_id:
            session = ExamSession.objects.filter(
                session_id=session_id,
                user=request.user,
            ).first()
        elif mode:
            session = (
                ExamSession.objects
                .filter(user=request.user, mode=mode, is_active=True)
                .order_by('-created_at')
                .first()
            )
            if not session:
                session = (
                    ExamSession.objects
                    .filter(user=request.user, mode=mode)
                    .order_by('-created_at')
                    .first()
                )
        else:
            session = None

        if not session:
            return api_success(data={
                'is_active': False,
                'question_ids': [],
                'current_index': 0,
                'answers': {},
                'tag': '',
                'started_at': None,
                'accumulated_time': 0,
            })

        return api_success(data=_session_progress_payload(session))
