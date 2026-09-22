# backend/apps/learning/views.py

from django.db.models import Q
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.questions.models import Question
from apps.questions.serializers import QuestionSerializer
from apps.core.utils import api_success, paginate, safe_int

from .srs_service import SRSService
from .services import LearningService


# ═════════════════════════════════════════════════════════════════════
# Shared queryset — eliminates the per-row relation N+1
# ═════════════════════════════════════════════════════════════════════
#
# `WrongAnswersView` and `FragileAnswersView` both serialize the
# results through `QuestionSerializer`. That serializer reads, on
# every row:
#
#   • category.name / category.color       → one query per question
#   • authored_by.username                 → one query per question
#   • owned_by.username                    → one query per question
#   • case.title / case.key / case.stem    → one query per question
#   • tags                                 → one query per question
#   • case_sibling_count                   → one COUNT per question
#
# Neither view loaded any of those relations, so a page of N
# questions fired roughly 6·N extra queries. The two querysets are
# identical except for the attempt filter, so the select_related /
# prefetch_related chain lives in one helper both callers use —
# matching the pattern already established by
# `QuestionService._visible_qs` and `QuestionListView`.
#
# `build_case_sibling_cache` (called from `_serialize_question_page`
# below) collapses the per-row `case_sibling_count` COUNT into a
# single grouped query for the whole page.

def _attempt_filtered_questions(user, attempt_filter):
    """
    Return the base `Question` queryset for one of the two personal
    review lists, with every relation the serializer reads joined or
    prefetched.

    `attempt_filter` is a `Q` object applied against the
    `user_attempts` reverse relation — the two views pass a filter
    for "ever wrong" and "fragile correct" respectively.

    `.distinct()` is retained even though the `(user, question)`
    unique constraint on `UserQuestionAttempt` means each user has at
    most one attempt row per question (so the join cannot duplicate).
    Keeping it means a future schema change that relaxes the
    constraint — or an additional join added on top — cannot silently
    produce duplicate rows.
    """
    return (
        Question.objects
        .visible_to(user)
        .filter(attempt_filter)
        .select_related('category', 'authored_by', 'owned_by', 'case')
        .prefetch_related('tags')
        .distinct()
        .order_by('-created_at')
    )


def _serialize_question_page(page, user, request):
    """
    Wrap one paginated slice of questions in a `QuestionSerializer`
    with the case-sibling cache pre-populated.

    Centralized so both list views and any future personal-review
    list share the exact same response shape and the same single-
    query sibling-count lookup.
    """
    page_list = list(page)
    cache = QuestionSerializer.build_case_sibling_cache(page_list, user)
    return QuestionSerializer(
        page_list, many=True,
        context={'request': request, 'case_sibling_cache': cache},
    )


class WrongAnswersView(APIView):
    """
    Questions the caller has attempted and never answered correctly.

    PERF (fix — N+1 on the serializer relations)
    --------------------------------------------
    The queryset now selects `category`, `authored_by`, `owned_by`,
    and `case`, and prefetches `tags`, so `QuestionSerializer` does
    not fire a per-row relation query. The case-sibling count is
    resolved with one grouped query for the whole page via
    `build_case_sibling_cache`.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = _attempt_filtered_questions(
            request.user,
            Q(user_attempts__user=request.user,
              user_attempts__ever_correct=False),
        )
        page, meta = paginate(qs, request)
        serializer = _serialize_question_page(page, request.user, request)
        return api_success(data={'items': serializer.data, **meta})


class FragileAnswersView(APIView):
    """
    Questions the caller last answered correctly but was not
    confident about.

    PERF (fix — N+1 on the serializer relations)
    --------------------------------------------
    Same query-shape fix as `WrongAnswersView`: the serializer's
    relations are joined / prefetched, and the case-sibling count is
    resolved once per page.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = _attempt_filtered_questions(
            request.user,
            Q(user_attempts__user=request.user,
              user_attempts__last_correct=True,
              user_attempts__last_confidence=False),
        )
        page, meta = paginate(qs, request)
        serializer = _serialize_question_page(page, request.user, request)
        return api_success(data={'items': serializer.data, **meta})


class AttemptSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return api_success(data=SRSService.attempt_summary(request.user))


class SRSDueCountView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return api_success(data={'count': SRSService.due_count(request.user)})


class StudyNowView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        limit = safe_int(
            request.query_params.get('limit'), 20,
            minimum=1, maximum=200,
        )
        payload = LearningService.study_now_queue(request.user, limit=limit)
        return api_success(data=payload)