# backend/apps/master_exams/views/composition_views.py
"""
Composition endpoints for master exams: add / remove / reorder
questions, and create draft questions inline.

OPTIMISTIC LOCKING
------------------
The three through-table writers (add / remove / reorder) each accept
an optional `expected_version` in the POST body. When present it is
forwarded to the service, which CASes `MasterExam.version` before
writing and returns 409 on a stale read. See the service docstrings
for the exact semantics.

`MasterExamAddDraftView` deliberately does NOT carry an
`expected_version`. Draft creation is a create-style operation
against the through table, not a content edit of the exam's ordered
question list. The service still advances `MasterExam.version`
unconditionally, so a subsequent content edit that supplies a stale
`expected_version` — captured before the draft was added — will
correctly 409. Adding conflict detection to a create operation would
buy nothing a caller could act on.
"""
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.utils import api_success, api_error

from ..models import MasterExam
from ..serializers import (
    MasterExamAddQuestionsSerializer,
    MasterExamRemoveQuestionSerializer,
    MasterExamReorderSerializer,
    MasterExamDraftCreateSerializer,
    MasterExamDraftSerializer,
    MasterExamDetailSerializer,
)
from ..services import MasterExamService
from .common import _can_manage_exam
from ._error_map import exam_error_response


def _expected_version_from(validated_data):
    """
    Pull `expected_version` from validated serializer data, returning
    None when the caller omitted it or sent null. Centralized so the
    three call sites below cannot drift on the "null vs. missing"
    handling.
    """
    return validated_data.get('expected_version')


class MasterExamAddQuestionsView(APIView):
    """
    Append question ids to the exam's ordered list. Requires
    'master_exams.manage_own' plus authorship, or 'manage_any'.

    Duplicate ids are silently dropped by the service; ids the caller
    cannot see (private drafts of another author) are rejected as a
    group with a 400.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        if not _can_manage_exam(request.user, exam):
            return api_error('غير مصرح لك', 403)

        serializer = MasterExamAddQuestionsSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        try:
            exam = MasterExamService.add_questions(
                exam,
                serializer.validated_data['question_ids'],
                request=request,
                expected_version=_expected_version_from(serializer.validated_data),
            )
        except ValueError as e:
            msg, status, details = exam_error_response(str(e))
            return api_error(msg, status, details=details)

        return api_success(
            data=MasterExamDetailSerializer(exam, context={'request': request}).data
        )


class MasterExamRemoveQuestionView(APIView):
    """
    Remove one question id from the exam's list. Requires
    'master_exams.manage_own' plus authorship, or 'manage_any'.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        if not _can_manage_exam(request.user, exam):
            return api_error('غير مصرح لك', 403)

        serializer = MasterExamRemoveQuestionSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        try:
            exam = MasterExamService.remove_question(
                exam,
                serializer.validated_data['question_id'],
                request=request,
                expected_version=_expected_version_from(serializer.validated_data),
            )
        except ValueError as e:
            msg, status, details = exam_error_response(str(e))
            return api_error(msg, status, details=details)

        return api_success(
            data=MasterExamDetailSerializer(exam, context={'request': request}).data
        )


class MasterExamReorderView(APIView):
    """
    Replace the exam's question order with a permuted version of the
    existing list. The service rejects any input that is not a
    permutation of the current list.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        if not _can_manage_exam(request.user, exam):
            return api_error('غير مصرح لك', 403)

        serializer = MasterExamReorderSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        try:
            exam = MasterExamService.reorder_questions(
                exam,
                serializer.validated_data['question_ids'],
                request=request,
                expected_version=_expected_version_from(serializer.validated_data),
            )
        except ValueError as e:
            msg, status, details = exam_error_response(str(e))
            return api_error(msg, status, details=details)

        return api_success(
            data=MasterExamDetailSerializer(exam, context={'request': request}).data
        )


class MasterExamAddDraftView(APIView):
    """
    Create a draft question scoped to this exam's author and append
    it to the exam. Requires 'master_exams.manage_own' plus
    authorship, or 'manage_any'.

    The category id is resolved to a Category instance here (the
    service layer expects the model, the serializer emits the id).
    A missing or invalid category is stored as null rather than
    rejected.

    The optional `case_key` is passed through to the service, which
    resolves or creates the case. The optional `case_order` is
    forwarded to the created Question row — see the service
    docstring for the behavior change.

    No `expected_version` is accepted here — see the module docstring
    for why draft creation does not participate in optimistic
    locking. The service still advances `MasterExam.version`, so a
    later content edit with a stale `expected_version` will see a
    conflict.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exam = get_object_or_404(MasterExam, pk=pk)
        if not _can_manage_exam(request.user, exam):
            return api_error('غير مصرح لك', 403)

        serializer = MasterExamDraftCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        payload = dict(serializer.validated_data)
        category_id = payload.pop('category', None)
        if category_id:
            from apps.questions.models import Category
            category = Category.objects.filter(id=category_id).first()
        else:
            category = None
        payload['category'] = category

        # `case_order` is left in the payload; the service pops it and
        # writes it to the Question row. See composition.add_draft.
        #
        # The only ValueError the service can raise on this path is
        # 'EXAM_WINDOW_STARTED' — there is no version CAS here, so
        # 'MODIFIED_BY_ANOTHER_USER' is unreachable. The shared
        # error map handles it anyway (unreachable entries are
        # harmless), so no special-casing is required.
        try:
            draft = MasterExamService.add_draft(
                exam, payload, request.user, request=request,
            )
        except ValueError as e:
            msg, status, details = exam_error_response(str(e))
            return api_error(msg, status, details=details)

        return api_success(
            data=MasterExamDraftSerializer(draft).data,
            message='تمت إضافة السؤال المؤقت',
            code=201,
        )