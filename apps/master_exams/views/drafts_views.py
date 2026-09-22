# backend/apps/master_exams/views/drafts_views.py
"""
Draft-question library for master-exam authors.

Draft questions are private to their author (or visible to admins
via 'manage_any'). They only become public bank content when the
exam is either deleted with delete_mode='publish_drafts' or
completed and published to the bank.
"""
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView

from apps.core.utils import api_success, api_error
from apps.core.permissions import HasCapability
from apps.questions.models import Question

from ..models import MasterExam, MasterExamQuestion
from ..serializers import (
    MasterExamDraftSerializer,
    MasterExamDraftCreateSerializer,
)


class MasterExamDraftsLibraryView(APIView):
    """
    List the caller's draft questions, filterable by search term and
    by whether the draft is currently attached to any non-cancelled
    exam.

    Requires 'master_exams.drafts_library'. Admins see every draft;
    everyone else sees only their own.
    """
    permission_classes = [HasCapability]
    required_capability = 'master_exams.drafts_library'

    def get(self, request):
        # An admin (through 'manage_any') sees every draft. Any other
        # holder of 'drafts_library' sees only their own.
        if request.user.has_capability('master_exams.manage_any'):
            qs = Question.objects.filter(is_draft=True)
        else:
            qs = Question.objects.filter(is_draft=True, draft_owner=request.user)

        search = (request.query_params.get('search') or '').strip()
        if len(search) >= 2:
            qs = qs.filter(
                Q(question__icontains=search) |
                Q(explanation__icontains=search) |
                Q(source__icontains=search)
            ).distinct()

        usage = request.query_params.get('usage')
        if usage in ('orphan', 'attached'):
            # One pass over every exam's through-relation question ids.
            # `MasterExam.question_ids` is a Python property now, so
            # iterate the relation directly: values_list on a
            # non-existent JSONField column would raise FieldError.
            all_ids = set()
            for qid in (
                MasterExam.objects
                .exclude(stored_status='cancelled')
                .values_list('exam_questions__question_id', flat=True)
            ):
                if qid is not None:
                    all_ids.add(qid)

            if usage == 'orphan':
                qs = qs.exclude(id__in=all_ids)
            else:
                qs = qs.filter(id__in=all_ids)

        qs = qs.select_related('case', 'category').prefetch_related('tags').order_by('-created_at')
        serializer = MasterExamDraftSerializer(qs, many=True)
        return api_success(data={
            'items': serializer.data,
            'total': qs.count(),
        })


class MasterExamDraftDetailView(APIView):
    """
    Read / update / delete a single draft.

    Authors can only touch their own drafts; holders of
    'master_exams.manage_any' (admins) can touch any draft. A draft
    cannot be deleted while attached to a non-cancelled exam — it
    must be removed from the exam first, so the deletion cannot
    silently break a live exam.

    DELETE BEHAVIOR (fixed)
    -----------------------
    `MasterExamQuestion.question` uses on_delete=PROTECT, so a
    MasterExamQuestion row referencing the draft blocks a raw
    `draft.delete()` even when the attached exam is cancelled or
    published_to_bank. The previous implementation checked only the
    non-terminal-status case and returned 409 for it; a draft
    attached solely to a cancelled exam passed the check and then
    raised ProtectedError → unhandled 500.

    The delete now:
      1. Blocks if the draft is attached to any live (non-cancelled,
         non-published-to-bank) exam, as before.
      2. Otherwise detaches the draft from every remaining through-
         row inside the same transaction, then deletes it.

    This preserves the original intent — "you cannot pull a draft
    out from under a running exam, but a cancelled exam's drafts are
    yours to clean up" — while making the cleanup path actually work.

    CASE_ORDER ON UPDATE (fixed)
    ----------------------------
    The PUT handler now assigns `draft.case_order` from the payload
    when the caller supplied the field. Previously the field was
    accepted by `MasterExamDraftCreateSerializer` and then silently
    discarded, so the stored ordering never matched the accepted
    input. Sending `case_order: null` explicitly clears the value,
    which matches the model's nullable semantics.
    """
    permission_classes = [HasCapability]
    required_capability = 'master_exams.drafts_library'

    def _can_edit(self, user, draft):
        if user.has_capability('master_exams.manage_any'):
            return True
        return draft.draft_owner_id == user.id

    def get(self, request, draft_id):
        draft = get_object_or_404(
            Question.objects.select_related('case', 'category').prefetch_related('tags'),
            id=draft_id,
            is_draft=True,
        )
        if not self._can_edit(request.user, draft):
            return api_error('غير مصرح لك', 403)
        return api_success(data=MasterExamDraftSerializer(draft).data)

    def put(self, request, draft_id):
        draft = get_object_or_404(
            Question.objects.select_related('case'),
            id=draft_id,
            is_draft=True,
        )
        if not self._can_edit(request.user, draft):
            return api_error('غير مصرح لك', 403)

        serializer = MasterExamDraftCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        payload = dict(serializer.validated_data)
        category_id = payload.pop('category', None)
        category = None
        if category_id:
            from apps.questions.models import Category
            category = Category.objects.filter(id=category_id).first()

        # Case linkage.
        #
        # The serializer declares both `case_key` and `case_stem`. We
        # only touch the FK when `case_key` was actually present in
        # the request body — an edit that omits the field must not
        # detach an existing case. Passing `case_key: null` or
        # `case_key: ""` explicitly detaches.
        #
        # The stem is passed as a hint to _resolve_case, which only
        # applies it when the target case has no stem yet. Editing
        # the stem on a populated case goes through the dedicated
        # /questions/case/<key>/stem/ endpoint so concurrent stem
        # edits serialize on the case row rather than racing on
        # individual questions.
        case_key_present = 'case_key' in request.data
        case_key = payload.pop('case_key', None)
        case_stem = payload.pop('case_stem', None)
        # `case_order` is a plain scalar on the Question row — pull
        # it out of the payload and apply it below. We distinguish
        # "not provided" from "provided as null" so an edit that
        # omits the key does not accidentally clear the ordering.
        case_order_present = 'case_order' in request.data
        case_order = payload.pop('case_order', None)

        if case_key_present:
            from apps.questions.serializers import _resolve_case
            draft.case = _resolve_case(
                case_key,
                request.user,
                stem=case_stem,
                existing_case=draft.case,
            )

        draft.question = payload['question']
        draft.choices = payload['choices']
        draft.correct_answer = payload['correct_answer']
        draft.explanation = payload.get('explanation') or ''
        draft.source = payload.get('source') or None
        draft.difficulty = payload.get('difficulty', 'medium')
        draft.category = category

        if case_order_present:
            draft.case_order = case_order

        draft.save()
        return api_success(data=MasterExamDraftSerializer(draft).data)

    def delete(self, request, draft_id):
        draft = get_object_or_404(Question, id=draft_id, is_draft=True)
        if not self._can_edit(request.user, draft):
            return api_error('غير مصرح لك', 403)

        # A draft is "in use" if any through-row references it AND the
        # exam is in a state where the question is part of a live
        # experience. Cancelled and published_to_bank exams are
        # terminal — their through-rows are historical artefacts, not
        # active attachments, so the draft can be cleaned up.
        blocking = (
            MasterExam.objects
            .exclude(stored_status__in=('cancelled', 'published_to_bank'))
            .filter(exam_questions__question_id=draft.id)
            .exists()
        )
        if blocking:
            return api_error(
                'الموضوع مرتبط بامتحان قائم — احذفه من الامتحان أولاً',
                409,
            )

        with transaction.atomic():
            # Detach from any terminal-state exam before deleting the
            # question. PROTECT on MasterExamQuestion.question would
            # otherwise raise ProtectedError for the very rows the
            # blocking check above intentionally allowed.
            MasterExamQuestion.objects.filter(question_id=draft.id).delete()
            draft.delete()

        return api_success(message='تم حذف المسودة')