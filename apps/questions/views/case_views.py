# backend/apps/questions/views/case_views.py

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from ..models import ClinicalCase, Question
from ..serializers import (
    ClinicalCaseSerializer,
    CaseStemUpdateSerializer,
    QuestionSerializer,
)
from ..services import QuestionService
from apps.core.utils import api_success, api_error
from apps.core.audit import log_privileged_action
from ..case_policy import can_edit_case


# ── Read-path visibility helper ─────────────────────────────────────────
#
# The case read endpoints scope results by this helper so a caller
# without any stake in a case cannot enumerate it. It is applied to
# every case read AND to every case write pre-check so an invisible
# case is indistinguishable from a nonexistent one — no 403-vs-404
# enumeration oracle.


def _visible_cases_qs(user):
    """Scope a ClinicalCase queryset to the cases `user` may read.

    A case is visible when ANY of the following holds:

      • The caller holds 'questions.edit_case_stem_any' — moderators
        and admins inspect every case, including drafts.
      • The caller authored the case directly (`authored_by`).
      • The case carries at least one question the caller can see —
        a public (non-draft) question, or the caller's own draft.

    The `.distinct()` is retained as a guard in case a future edit
    replaces the `id__in` subquery with a `questions__…` traversal.
    """
    if user.has_capability('questions.edit_case_stem_any'):
        return ClinicalCase.objects.all()

    visible_case_ids = (
        Question.objects
        .visible_to(user)
        .exclude(case_id__isnull=True)
        .values_list('case_id', flat=True)
    )
    return ClinicalCase.objects.filter(
        Q(authored_by=user) | Q(id__in=visible_case_ids)
    ).distinct()


def _visible_questions_in_case(user, case_id):
    """
    Return a question queryset scoped to what `user` may see inside
    a single case.

    TWO-TIER SCOPING — WHY `edit_any`, NOT `edit_case_stem_any`
    ----------------------------------------------------------
    The codebase treats `questions.edit_any` (and its sibling
    `questions.delete_any`) as the "see every question, including
    other authors' private drafts" flag — see
    `_fetch_question_for_action` in
    `apps/questions/views/question_views.py`, which uses exactly
    that rule. `questions.edit_case_stem_any` is a different
    capability: it means "may edit the shared vignette on any
    case", and the codebase nowhere treats it as a question
    visibility override.

    Using `edit_case_stem_any` here would work today by accident —
    in the default role map, moderators and admins hold both — but
    it would silently break the moment a deployment split the two
    capabilities onto different roles. `edit_any` is the honest
    flag and matches the rule the rest of the read paths follow.

    Used by `CaseDetailView.delete` and exposed as a helper so the
    scoping rule has one definition per module.
    """
    qs = Question.objects.all()
    if not user.has_capability('questions.edit_any'):
        qs = qs.visible_to(user)
    return qs.filter(case_id=case_id)


class CaseListView(APIView):
    """
    List cases for the case-picker autocomplete. Any authenticated
    user — the picker is part of the question authoring form.

    Query params:
        search  — substring match against key or title (>= 1 char)
        limit   — capped at 100, default 50

    QUESTION COUNT SCOPING
    ----------------------
    The `question_count` field on each item reports the number of
    questions in the case that THE CALLER CAN SEE — public questions
    plus the caller's own drafts. Without this scope, the field
    counted every question attached to the case, including other
    authors' private drafts, and the count alone told the caller
    how many drafts someone else had stashed in a case they
    otherwise had legitimate access to.

    The count is pre-computed with a single `Count(..., filter=...)`
    annotation over the page, so the response stays at one query for
    the cases plus one query for the counts, regardless of page
    size. `ClinicalCaseSerializer.get_question_count` reads the
    `question_count_visible` annotation when present.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        search = (request.query_params.get('search') or '').strip()
        try:
            limit = int(request.query_params.get('limit', 50))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 100))

        # Subquery form so the count follows `visible_to`'s rule
        # verbatim. If `visible_to` ever changes (e.g. a capability
        # bypass for a future reviewer role), this count follows
        # without a second edit.
        visible_question_ids = (
            Question.objects
            .visible_to(request.user)
            .values_list('id', flat=True)
        )

        qs = _visible_cases_qs(request.user).annotate(
            question_count_visible=Count(
                'questions',
                filter=Q(questions__id__in=visible_question_ids),
            ),
        )

        if len(search) >= 1:
            qs = qs.filter(
                Q(key__icontains=search) | Q(title__icontains=search),
            )

        total = qs.count()
        items = qs.order_by('key')[:limit]

        serializer = ClinicalCaseSerializer(
            items, many=True,
            context={'request': request},
        )
        return api_success(data={
            'items': serializer.data,
            'total': total,
        })


class CaseDetailView(APIView):
    """
    Read / update / delete a single case.

    GET    — any authenticated user with visibility on the case.
    PUT    — gated by the same two-tier rule as the stem endpoint
             (edit_case_stem_any, or edit_case_stem_own + authorship).
             Only `title` and `stem` are writable here; the key is
             immutable once created because it is referenced by URL.
    DELETE — requires 'questions.edit_case_stem_any' (moderator or
             admin). Questions are NOT deleted; the FK uses SET_NULL,
             so they become standalone.

    AUDIT PARITY WITH CaseStemUpdateView (fix)
    ------------------------------------------
    The `stem` branch of this view's `put` used to call
    `QuestionService.update_case_stem(...)` without writing a
    `log_privileged_action` row. The dedicated
    `CaseStemUpdateView.post` endpoint edits the SAME field on the
    same case via the same service call and DOES write an audit row.
    So an edit that ran through `/case/<key>/` left no trace, while
    an identical edit that ran through `/case/<key>/stem/` did.

    This view now writes an audit row in the same shape (same
    action name, same target_repr format, same `updated_count` and
    `stem_length` details keys) so the two write surfaces cannot
    produce different audit trails for the same change.

    DELETE — SCOPED COUNT (this revision)
    -------------------------------------
    The `affected` value returned in `{'detached_questions': N}` and
    rendered into the success message used to be
    `case.questions.count()` — the total across every author. A
    caller who could see the case (they hold
    `questions.edit_case_stem_any`) but not every draft in it (they
    do NOT hold `questions.edit_any`) learned, from the number
    alone, how many other authors' private drafts were stashed in
    the case.

    The count now follows `_visible_questions_in_case`, matching the
    scoping every other read path in this module uses. A moderator
    holding `edit_any` — the default role map gives moderators and
    admins both — still receives the true count, because
    `_visible_questions_in_case` short-circuits to
    `Question.objects.all()` for them.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, case_key):
        case = get_object_or_404(_visible_cases_qs(request.user), key=case_key)
        case_data = ClinicalCaseSerializer(
            case, context={'request': request},
        ).data

        # Scope the sibling-question list by the same question
        # visibility rule the rest of the app uses.
        questions = (
            Question.objects
            .visible_to(request.user)
            .filter(case=case)
            .select_related('category', 'authored_by', 'owned_by')
            .prefetch_related('tags')
        )
        page_list = list(questions)
        cache = QuestionSerializer.build_case_sibling_cache(page_list, request.user)
        case_data['questions'] = QuestionSerializer(
            page_list, many=True,
            context={'request': request, 'case_sibling_cache': cache},
        ).data
        return api_success(data=case_data)

    def put(self, request, case_key):
        case = get_object_or_404(_visible_cases_qs(request.user), key=case_key)

        if 'stem' in request.data:
            new_stem = request.data.get('stem')
            try:
                updated = QuestionService.update_case_stem(
                    case.key, new_stem, request.user,
                )
                case.refresh_from_db()
            except PermissionError:
                return api_error('غير مصرح لك بتعديل نص هذه الحالة', 403)

            # Audit row — byte-identical to the one CaseStemUpdateView.post
            # writes for the same operation.
            log_privileged_action(
                request,
                action='case.stem_update',
                target=None,
                target_repr=f'case:{case_key}',
                details={
                    'updated_count': updated,
                    'stem_length': len(new_stem or ''),
                },
            )

        if 'title' in request.data:
            if not can_edit_case(request.user, case):
                return api_error('غير مصرح لك بتعديل هذه الحالة', 403)
            new_title = (request.data.get('title') or '').strip() or None
            case.title = new_title
            case.save(update_fields=['title', 'updated_at'])

        return api_success(
            data=ClinicalCaseSerializer(
                case, context={'request': request},
            ).data,
        )

    def delete(self, request, case_key):
        if not request.user.has_capability('questions.edit_case_stem_any'):
            return api_error('غير مصرح لك بحذف هذه الحالة', 403)

        # The caller passed the capability gate above, so
        # `_visible_cases_qs` returns everything for them — the
        # filter is a no-op here today. Kept for consistency with
        # the other methods so that if the delete capability is ever
        # narrowed, the visibility filter becomes load-bearing
        # without a second edit.
        case = get_object_or_404(_visible_cases_qs(request.user), key=case_key)

        # Scoped count. The FK uses SET_NULL, so deleting the case
        # detaches every question regardless of author. Reporting
        # the count of questions the caller could actually see is
        # the number this endpoint is entitled to disclose — see
        # the class docstring for the leak this closes.
        affected = _visible_questions_in_case(request.user, case.id).count()
        stored_key = case.key
        case.delete()

        log_privileged_action(
            request,
            action='case.delete',
            target=None,
            target_repr=f'case:{stored_key}',
            details={'detached_questions': affected},
        )

        return api_success(
            data={'detached_questions': affected},
            message=f'تم حذف الحالة ({affected} سؤال أصبح مستقلاً)',
        )


class CaseStemUpdateView(APIView):

    permission_classes = [IsAuthenticated]

    def post(self, request, case_key):
        serializer = CaseStemUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات غير صالحة', 400, details=serializer.errors)

        new_stem = serializer.validated_data['case_stem']

        # Visibility pre-filter. Uses the same scope the read
        # endpoints use, so an invisible case is indistinguishable
        # from a nonexistent one. The message matches the read
        # endpoints' 404 wording.
        if not _visible_cases_qs(request.user).filter(key=case_key).exists():
            return api_error('الحالة غير موجودة', 404)

        try:
            updated = QuestionService.update_case_stem(
                case_key, new_stem, request.user,
            )
        except PermissionError:
            return api_error('غير مصرح لك بتعديل نص هذه الحالة', 403)

        # `updated` is now the affected-question count, NOT a
        # success/failure signal. A zero is a valid outcome of a
        # successful write on an empty case — see the class
        # docstring. The audit row is written for both, because
        # both persisted a change to the case row.
        log_privileged_action(
            request,
            action='case.stem_update',
            target=None,
            target_repr=f'case:{case_key}',
            details={
                'updated_count': updated,
                'stem_length': len(new_stem or ''),
            },
        )

        if updated:
            message = f'تم تحديث نص الحالة في {updated} سؤال'
        else:
            # The stem was still written; the caller just has no
            # questions riding on it right now.
            message = 'تم تحديث نص الحالة (لا توجد أسئلة مرتبطة حالياً)'

        return api_success(
            data={'updated': updated, 'case_stem': new_stem},
            message=message,
        )