# backend/apps/feedback/views.py

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from django.db.models import Avg, Count
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.questions.models import Question
from apps.questions.serializers import QuestionSerializer
from apps.core.permissions import HasCapability
from apps.core.utils import api_success, api_error, dedupe_ordered, paginate

from .models import Bookmark, QuestionFlag, QuestionRating
from .serializers import FlagQuestionSerializer, RateQuestionSerializer
from .services import FeedbackService


def _visible_question_or_404(user, question_id):
    """
    Fetch a question the caller is allowed to see, or raise Http404.

    Using the same `visible_to` queryset the question list endpoints
    use means the visibility rule has exactly one definition in the
    codebase.
    """
    return get_object_or_404(
        Question.objects.visible_to(user),
        id=question_id,
    )


class BookmarkToggleView(APIView):
    """
    Any authenticated user can bookmark any VISIBLE question.

    A draft the caller does not own is invisible here — the previous
    implementation confirmed draft existence via the 404-vs-200
    difference and allowed the write itself, which leaked the
    draft's existence and (via the list endpoint below) its content.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, question_id):
        _visible_question_or_404(request.user, question_id)
        added = FeedbackService.toggle_bookmark(request.user.id, question_id)
        message = 'تمت إضافة السؤال إلى المفضلة' if added else 'تم إزالة السؤال من المفضلة'
        return api_success(data={'added': added}, message=message)


class BookmarkListView(APIView):
    """
    The caller's own bookmarks, filtered to questions the caller can
    currently see.

    A bookmark row's Question FK has no visibility restriction; the
    filter must be applied here. Without it, a user who bookmarked a
    question that later became invisible (e.g. a question that was
    somehow converted to another user's draft — not a state the
    current code produces, but the endpoint should not depend on that
    invariant) would still read the content.

    PERF (fix — N+1 on the question relations)
    ------------------------------------------
    The previous `select_related('question')` joined only the
    question itself. `QuestionSerializer` also reads, per row:
    `question.category`, `question.authored_by`, `question.owned_by`,
    `question.case`, and `question.tags`. Without those joins the
    list fired roughly 5 extra queries per bookmark.

    The chain is now expanded so all of those relations travel with
    the bookmark query. `question__tags` is a reverse-FK prefetch
    (multi-row); the others are forward FKs and are handled by
    `select_related`.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        visible_questions = Question.objects.visible_to(request.user)
        bookmarks = (
            Bookmark.objects
            .filter(user=request.user, question__in=visible_questions)
            .select_related(
                'question',
                'question__category',
                'question__authored_by',
                'question__owned_by',
                'question__case',
            )
            .prefetch_related('question__tags')
        )
        questions = [b.question for b in bookmarks]
        cache = QuestionSerializer.build_case_sibling_cache(questions, request.user)
        serializer = QuestionSerializer(
            questions, many=True,
            context={'request': request, 'case_sibling_cache': cache},
        )
        return api_success(data={
            'items': serializer.data,
            'count': len(serializer.data),
        })


class BookmarkCountView(APIView):
    """
    Count of the caller's VISIBLE bookmarks. Used by the dashboard
    tile.

    The count excludes bookmarks on questions the caller cannot see,
    so it stays consistent with the list endpoint above.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        visible_questions = Question.objects.visible_to(request.user)
        count = Bookmark.objects.filter(
            user=request.user,
            question__in=visible_questions,
        ).count()
        return api_success(data={'count': count})


class FlagQuestionView(APIView):
    """
    File a report against a question. The unique constraint
    `unique_open_flag_per_user_question` allows one unresolved flag
    per (user, question); a repeat submission returns 400 rather
    than creating a duplicate.

    Visibility gate applied: an invisible question returns the same
    404 as a nonexistent one, so a caller cannot enumerate other
    authors' draft ids by probing the flag endpoint.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, question_id):
        body = FlagQuestionSerializer(data=request.data)
        if not body.is_valid():
            return api_error('بيانات غير صالحة', 400, details=body.errors)
        _visible_question_or_404(request.user, question_id)
        success = FeedbackService.flag_question(
            request.user.id, question_id, body.validated_data['reason'],
        )
        if not success:
            return api_error('لقد قمت بالإبلاغ عن هذا السؤال بالفعل', 400)
        return api_success(message='تم الإبلاغ عن السؤال للمراجعة', code=201)


class RateQuestionView(APIView):
    """
    Upsert the caller's rating for a question. 1 to 5.

    Visibility gate applied for the same reason as FlagQuestionView.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, question_id):
        body = RateQuestionSerializer(data=request.data)
        if not body.is_valid():
            return api_error('التقييم يجب أن يكون بين 1 و 5', 400, details=body.errors)
        _visible_question_or_404(request.user, question_id)
        rating_obj, created = QuestionRating.objects.update_or_create(
            question_id=question_id,
            user=request.user,
            defaults={'rating': body.validated_data['rating']},
        )
        return api_success(
            data={'rating': rating_obj.rating},
            message='تم التقييم',
        )


class GetQuestionRatingView(APIView):
    """
    The caller's rating plus the global average and count.

    Visibility gate applied so the aggregate count for an invisible
    question cannot be read. In practice this leaks only "this
    invisible question has N ratings"; the rate endpoint above is
    the write surface, and both are now consistent.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, question_id):
        _visible_question_or_404(request.user, question_id)
        user_rating = QuestionRating.objects.filter(
            question_id=question_id, user=request.user,
        ).first()
        aggregate = QuestionRating.objects.filter(
            question_id=question_id,
        ).aggregate(average=Avg('rating'), count=Count('id'))
        return api_success(data={
            'user_rating': user_rating.rating if user_rating else 0,
            'average': round(aggregate['average'] or 0, 2),
            'count': aggregate['count'],
        })


class QuestionRatingsBatchView(APIView):
    """
    Return the caller's rating plus the global average and count for
    a list of question ids in one round trip. Read-only.

    The visibility filter is already applied — `visible_to()` scopes
    the id resolution below, so an invisible id is silently dropped
    from the response.

    DEDUPE — shared with StartSessionView via `dedupe_ordered`.
    """
    permission_classes = [IsAuthenticated]

    MAX_IDS = 500

    def get(self, request):
        ids_raw = request.query_params.get('ids', '')
        parsed = []
        for piece in ids_raw.split(','):
            piece = piece.strip()
            if not piece:
                continue
            try:
                parsed.append(int(piece))
            except (TypeError, ValueError):
                continue

        # Deduplicate while preserving order — a caller may
        # accidentally include the same id twice, and the response
        # should not double-count it.
        unique_ids = dedupe_ordered(parsed)

        if not unique_ids:
            return api_success(data={'items': []})
        if len(unique_ids) > self.MAX_IDS:
            return api_error(
                f'الحد الأقصى {self.MAX_IDS} سؤال في الطلب الواحد',
                400,
            )

        visible_id_set = set(
            Question.objects
            .visible_to(request.user)
            .filter(id__in=unique_ids)
            .values_list('id', flat=True)
        )
        visible_ids = [qid for qid in unique_ids if qid in visible_id_set]
        if not visible_ids:
            return api_success(data={'items': []})

        # One aggregation over every requested question.
        agg_rows = (
            QuestionRating.objects
            .filter(question_id__in=visible_ids)
            .values('question_id')
            .annotate(average=Avg('rating'), count=Count('id'))
        )
        agg_map = {
            row['question_id']: {
                'average': round(row['average'] or 0, 2),
                'count': row['count'] or 0,
            }
            for row in agg_rows
        }

        # One query for the caller's own ratings across the batch.
        user_ratings = dict(
            QuestionRating.objects
            .filter(question_id__in=visible_ids, user=request.user)
            .values_list('question_id', 'rating')
        )

        items = [
            {
                'question_id': qid,
                'user_rating': user_ratings.get(qid, 0),
                'average': agg_map.get(qid, {}).get('average', 0),
                'count': agg_map.get(qid, {}).get('count', 0),
            }
            for qid in visible_ids
        ]
        return api_success(data={'items': items})


class AdminFlagListView(APIView):
    """
    Pending flag queue for the moderator. Requires 'admin.flags'.

    `question_author` is sourced from `Question.authored_by` — the
    field that records who wrote the question. Not `owned_by`:
    the moderator is triaging a report about the content, and the
    person responsible for the content is its author. A question
    whose `authored_by` is NULL (seed content, unresolved import)
    reports an empty string, which the frontend renders as
    "unknown author".

    `select_related('question__authored_by', 'user')` collapses the
    per-row author and flagger lookups into the main query.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.flags'

    def get(self, request):
        flags = (
            QuestionFlag.objects
            .filter(resolved=False)
            .select_related('question__authored_by', 'user')
            .order_by('-created_at')
        )
        page, meta = paginate(flags, request)
        data = []
        for flag in page:
            data.append({
                'id': flag.id,
                'question_id': flag.question_id,
                'user_id': flag.user_id,
                'reason': flag.reason,
                'created_at': flag.created_at.isoformat() if flag.created_at else None,
                'resolved': flag.resolved,
                'question_text': flag.question.question[:200] if flag.question else '',
                'question_author': (
                    flag.question.authored_by.username
                    if flag.question and flag.question.authored_by_id
                    else ''
                ),
                'flagger_username': flag.user.username if flag.user else '',
            })
        return api_success(data={'items': data, **meta})


class AdminResolveFlagView(APIView):
    """
    Close one flag without deleting it — the row is kept for audit.
    Requires 'admin.flags'.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.flags'

    def post(self, request, flag_id):
        flag = get_object_or_404(QuestionFlag, id=flag_id)
        flag.resolved = True
        flag.resolved_by = request.user.username
        flag.resolved_at = timezone.now()
        flag.save()
        return api_success(message='تم حل الإبلاغ')