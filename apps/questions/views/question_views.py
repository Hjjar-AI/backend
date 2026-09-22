# backend/apps/questions/views/question_views.py

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from django.shortcuts import get_object_or_404
from django.utils import timezone

from ..models import Question
from ..serializers import (
    QuestionSerializer,
    QuestionBatchSerializer,
    BulkVerifySerializer,
    BulkTagUpdateSerializer,
    QuestionImageUploadSerializer,
)
from ..services import QuestionService
from apps.core.permissions import HasCapability
from apps.core.utils import (
    api_success,
    api_error,
    dedupe_ordered,
    paginate,
    parse_csv_param,
)
from apps.core.throttles import BulkVerifyRateThrottle


# ═════════════════════════════════════════════════════════════════════
# Ownership helper — single source of truth
# ═════════════════════════════════════════════════════════════════════
#
# Three views in this module (put, delete, image upload) each needed
# the same rule: "allow if the caller holds the '_any' capability,
# otherwise allow if they hold the '_own' (or a specific action)
# capability AND own the question."
#
# The three call sites used only the capability STRINGS to differ —
# the shape of the check was identical. The helper below is now the
# one place it lives, so a change to the resolution rule (e.g.
# falling back to `authored_by` when `owned_by` is null) lands once.

def _is_own_question(question, user):
    """
    Identity-only ownership test.

    Ownership is resolved through `Question.owned_by` — the FK that
    records the current steward. NOT `authored_by`: a moderator who
    takes over someone else's question should be able to edit it,
    and a user who wrote a question but handed off maintenance should
    lose the edit gate without losing the credit.

    A question with `owned_by = NULL` is a draft whose creation has
    not finished, or a data error. Either way, no member "owns" it;
    moderators and admins can still reach it via `edit_any` /
    `delete_any`, which do not call this helper.
    """
    if question.owned_by_id is None:
        return False
    return question.owned_by_id == user.id


def _has_ownership_override(user, question, *, any_cap, own_cap):
    """
    True when `user` may act on `question`: either they hold
    `any_cap`, or they hold `own_cap` AND own the question.

    The two callers that use this helper for editing pass
    `own_cap='questions.edit_own'`; the one that uses it for image
    upload passes `own_cap='questions.upload_image'`. That is the
    whole reason the capability names are parameters.
    """
    if user.has_capability(any_cap):
        return True
    if user.has_capability(own_cap) and _is_own_question(question, user):
        return True
    return False


def _fetch_question_for_action(user, question_id, *, any_cap):
    """
    Fetch a question the caller may act on, without leaking the
    existence of drafts they cannot see.

    A caller holding `any_cap` (edit_any / delete_any) is allowed to
    act on any question, including drafts owned by other authors, so
    the lookup is unscoped for them. Every other caller is scoped to
    `Question.objects.visible_to(user)` — public questions plus their
    own drafts — so an inaccessible draft is indistinguishable from a
    nonexistent id (both return the same 404).

    Raises Http404 (via get_object_or_404) when the question does not
    exist OR the caller cannot see it.

    The subsequent `_has_ownership_override` check still returns 403
    for the case where the caller can see a question (a public one, or
    their own draft) but lacks the `_own`/`_any` capability that would
    let them act on it. That 403 is correct and expected — the
    question is visible to them, they simply may not modify it. Only
    the "you cannot even see this question" case is converted from
    403 to 404.
    """
    qs = Question.objects.all()
    if not user.has_capability(any_cap):
        qs = qs.visible_to(user)
    return get_object_or_404(qs, id=question_id)


class QuestionListView(APIView):
    """
    GET  — paginated, filterable public question list.
    POST — create a question. Requires 'questions.create'.

    FILTER SHAPES
    -------------
    Two category-filter shapes are accepted, matching the two shapes
    `AvailableCountView` already accepts:

      • `category_ids` — a comma-joined list of ints, e.g.
        `?category_ids=3,7,12`. This is what the study-setup screen
        sends when the user picks multiple categories from the
        checkbox grid. Passed through to
        `QuestionService.get_questions` as a list, which the service
        normalizes and applies as `category_id__in=[...]`.

      • `category`     — a single int. Legacy shape, kept for older
        clients that still have the single-category dropdown.

    The two are mutually exclusive in the service: when `category_ids`
    is present and non-empty, it wins and `category` is ignored. This
    matches the precedence rule in `QuestionService.get_questions`.

    REGRESSION NOTE
    ---------------
    An earlier revision of this view read only the singular `category`
    key. `category_ids` was silently ignored, so a caller that
    filtered the list endpoint by multiple categories received every
    public question instead. `AvailableCountView` — the endpoint the
    same UI calls first to decide whether enough questions exist for
    the selected filter — has always honored `category_ids`, so the
    count and the list disagreed. That disagreement is what the test
    `test_category_ids_array_filter` pins against.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        filters = {}
        for key in ['tag', 'category', 'difficulty', 'verified', 'search']:
            if key in request.query_params:
                filters[key] = request.query_params[key]

        # Multi-select category filter. Same parse shape as
        # AvailableCountView — a comma-joined list in the query
        # string. Empty pieces are dropped; a query string of
        # `?category_ids=` (present but empty) is treated as "no
        # filter" rather than "filter by zero categories", so the
        # service never sees an empty list.
        category_ids_raw = request.query_params.get('category_ids')
        if category_ids_raw:
            parsed = parse_csv_param(category_ids_raw)
            if parsed:
                filters['category_ids'] = parsed

        if 'tags_filter' in request.query_params:
            parsed = parse_csv_param(request.query_params['tags_filter'])
            if parsed:
                filters['tags_filter'] = parsed

        # `user=None` deliberately scopes the list to `.public()` —
        # drafts are excluded even for their own owner. The draft
        # workflow is served by the master-exam drafts library.
        queryset = QuestionService.get_questions(filters, user=None)

        page, meta = paginate(queryset, request)
        page_list = list(page)
        cache = QuestionSerializer.build_case_sibling_cache(page_list, request.user)
        serializer = QuestionSerializer(
            page_list, many=True,
            context={'request': request, 'case_sibling_cache': cache},
        )
        return api_success(data={'items': serializer.data, **meta})

    def post(self, request):
        if not request.user.has_capability('questions.create'):
            return api_error('غير مصرح لك', 403)
        question = QuestionService.create_question(request.data, request.user)
        return api_success(
            data=QuestionSerializer(question, context={'request': request}).data,
            message='تم إنشاء السؤال',
            code=201,
        )


class QuestionDetailView(APIView):
    """
    GET    — retrieve one question the caller is allowed to see.
    PUT    — edit. Requires 'questions.edit_any', or 'questions.edit_own'
             plus ownership.
    DELETE — delete. Requires 'questions.delete_any', or
             'questions.delete_own' plus ownership, plus no live master
             exam currently referencing the question.

    VISIBILITY SCOPING (fix — draft-existence oracle)
    -------------------------------------------------
    `put` and `delete` used to do a plain
    `get_object_or_404(Question, id=question_id)`. That lookup
    confirms the row exists but not that the caller can see it, so a
    member could distinguish "another author's draft exists" (403
    from the ownership check) from "id does not exist" (404 from the
    fetch) — an existence oracle over every other author's draft ids.

    Both methods now route the fetch through
    `_fetch_question_for_action`, which scopes the lookup to
    `visible_to(user)` unless the caller holds the matching `_any`
    capability. A holder of `edit_any` / `delete_any` still reaches
    any question; every other caller gets a 404 for a question they
    cannot see, matching the response shape a nonexistent id
    produces.

    The 403 for "question is visible to you but you lack the
    capability to modify it" is preserved unchanged.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, question_id):
        question = QuestionService.get_question(question_id, user=request.user)
        if question is None:
            return api_error('السؤال غير موجود', 404)
        serializer = QuestionSerializer(question, context={'request': request})
        return api_success(data=serializer.data)

    def put(self, request, question_id):
        # Scoped fetch: 404 for a draft the caller cannot see, same as
        # a nonexistent id. Callers holding 'questions.edit_any' still
        # reach any row.
        question = _fetch_question_for_action(
            request.user, question_id,
            any_cap='questions.edit_any',
        )

        if not _has_ownership_override(
            request.user, question,
            any_cap='questions.edit_any',
            own_cap='questions.edit_own',
        ):
            return api_error('غير مصرح لك بتعديل هذا السؤال', 403)

        try:
            updated = QuestionService.update_question(
                question_id, request.data, request.user,
            )
            return api_success(
                data=QuestionSerializer(updated, context={'request': request}).data,
            )
        except ValueError as e:
            if "modified by another user" in str(e):
                return api_error(str(e), 409)
            return api_error(str(e), 400)

    def delete(self, request, question_id):
        # Scoped fetch — same rationale as `put` above. Uses
        # 'questions.delete_any' as the bypass capability so a
        # moderator who can delete any question can still reach a
        # draft the visibility filter would otherwise hide.
        question = _fetch_question_for_action(
            request.user, question_id,
            any_cap='questions.delete_any',
        )

        if not _has_ownership_override(
            request.user, question,
            any_cap='questions.delete_any',
            own_cap='questions.delete_own',
        ):
            return api_error('غير مصرح لك بحذف هذا السؤال', 403)

        # A question referenced by a master exam cannot be deleted.
        # The FK on MasterExamQuestion uses PROTECT, so a raw
        # Question.delete() would raise ProtectedError. Catching that
        # and returning 409 is cleaner than a pre-flight scan, and it
        # is race-free: the PROTECT fires inside the transaction that
        # deletes the row.
        from django.db.models import ProtectedError

        try:
            QuestionService.delete_question(question_id)
        except ProtectedError:
            return api_error(
                'هذا السؤال مستخدم في امتحان رئيسي — احذفه من الامتحان أولاً',
                409,
            )

        return api_success(message='تم حذف السؤال')


class QuestionDuplicateView(APIView):
    """
    Create a copy of an existing question, including its case linkage.
    Requires 'questions.duplicate'.

    VISIBILITY GATE
    ---------------
    The lookup is scoped to `Question.objects.visible_to(user)`, so a
    draft the caller does not own is indistinguishable from a
    nonexistent id.

    ATOMICITY (fix — three writes across two tables with no transaction)
    -------------------------------------------------------------------
    The view used to hand-roll the copy directly:

        new_q = Question.objects.create(...)          # write 1
        new_q.tags.set(original.tags.all())           # write 2 (M2M)
        request.user.update_trust_score()             # write 3 (User)

    ...with no `transaction.atomic()` anywhere in the method. A crash
    between writes 1 and 2 produced a copy with no tags. A crash
    between 2 and 3 produced a copy whose author's `questions_count`
    had not been incremented. On retry, the client created a second
    copy — the intermediate state did not self-heal, it doubled.

    The write is now delegated to `QuestionService.duplicate_question`,
    which wraps the whole sequence in one atomic block and lives
    alongside every other question-write operation in the codebase
    (`create_question`, `update_question`, `delete_question`,
    `toggle_verify`, `bulk_verify`, `bulk_unverify`). The view keeps
    the capability check and the visibility-scoped fetch.

    DRAFT STATE OF THE COPY
    -----------------------
    If the original is a draft the caller owns, the copy is also a
    draft owned by the caller. If the original is public, the copy
    is public. Copying a private draft into the public bank would
    be a surprising way to leak it — even the caller's own drafts.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, question_id):
        if not request.user.has_capability('questions.duplicate'):
            return api_error('غير مصرح لك', 403)

        original = get_object_or_404(
            Question.objects.select_related('case').visible_to(request.user),
            id=question_id,
        )

        new_q = QuestionService.duplicate_question(original, request.user)

        return api_success(
            data=QuestionSerializer(new_q, context={'request': request}).data,
            message='تم نسخ السؤال',
            code=201,
        )


class QuestionBatchView(APIView):
    """
    Hydrate a list of question ids in one round-trip. Used by the
    Bookmarks and Wrong-Answers views to materialize ids into full
    question payloads. Read-only.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        body = QuestionBatchSerializer(data=request.data)
        if not body.is_valid():
            return api_error('معرفات غير صالحة', 400, details=body.errors)
        ids = dedupe_ordered(body.validated_data['ids'])
        if len(ids) > 500:
            return api_error('الحد الأقصى 500 سؤال', 400)
        questions = list(
            Question.objects
            .visible_to(request.user)
            .filter(id__in=ids)
            .select_related('case', 'category', 'authored_by', 'owned_by')
            .prefetch_related('tags')
        )
        cache = QuestionSerializer.build_case_sibling_cache(questions, request.user)
        out = QuestionSerializer(
            questions, many=True,
            context={'request': request, 'case_sibling_cache': cache},
        )
        return api_success(data={'items': out.data, 'total': len(out.data)})


class ToggleVerifyView(APIView):
    """
    Flip the verified flag on one question. Requires
    'questions.verify'.

    VISIBILITY SCOPING (fix — draft-existence oracle)
    -------------------------------------------------
    The lookup used to be a plain `get_object_or_404(Question, id=...)`.
    A caller holding 'questions.verify' (moderator by default) could
    probe `/questions/<id>/verify/` and distinguish "another author's
    draft exists" (200) from "id does not exist" (404), leaking the
    existence of drafts they cannot see. The same class of bug was
    fixed for `QuestionDetailView.put/delete` and
    `QuestionImageUploadView.post` via `_fetch_question_for_action`;
    this view was missed.

    The fetch is now scoped to `visible_to(user)` — public questions
    plus the caller's own drafts. There is no `_any` bypass here
    because `questions.verify` has no `_own`/`_any` split in
    `apps/users/capabilities.py`; the capability itself is the
    override, and its reach is limited to visible questions. A
    moderator who wants to reach another author's draft must use
    `edit_any` / `delete_any` through the views that already expose
    those scopes.

    ATOMICITY (fix — two writes across two tables with no transaction)
    -----------------------------------------------------------------
    The view used to run the flip and the trust recompute as two
    independent statements:

        question.save()
        if question.authored_by_id:
            question.authored_by.update_trust_score()

    ...with no `transaction.atomic()` around either. If the second
    failed, the question was left marked verified with no
    trust-score credit — and because the endpoint TOGGLES, a client
    retry flipped the flag back rather than re-attempting the
    recompute. The intermediate state was inconsistent AND
    self-correcting in the wrong direction.

    The write is now delegated to `QuestionService.toggle_verify`,
    which wraps both writes in one atomic block — matching
    `QuestionService.create_question` and `bulk_verify`.
    """
    permission_classes = [HasCapability]
    required_capability = 'questions.verify'

    def post(self, request, question_id):
        question = get_object_or_404(
            Question.objects.visible_to(request.user),
            id=question_id,
        )

        new_verified = QuestionService.toggle_verify(question, request.user)

        message = (
            'تم تدقيق السؤال' if new_verified
            else 'تم إلغاء تدقيق السؤال'
        )
        return api_success(data={'verified': new_verified}, message=message)


class BulkVerifyView(APIView):
    """
    Verify or unverify up to 500 questions in one call. Requires
    'questions.bulk_verify'.

    VISIBILITY SCOPING (fix — draft-existence oracle)
    -------------------------------------------------
    The view used to pass the caller-supplied id list straight to
    `QuestionService.bulk_verify`, which ran a bare
    `Question.objects.filter(id__in=question_ids).update(...)`. A
    caller holding 'questions.bulk_verify' could therefore verify
    another author's private drafts (by observing the returned count),
    and the update itself would flip a row the caller could not see.

    The id list is now scoped to `visible_to(user)` before reaching the
    service. Ids the caller cannot see are silently dropped from the
    operation — matching the way `QuestionBatchView` already treats
    invisible ids. If NO id survives filtering, the view returns 400
    rather than a silent count of zero, so a caller that sends only
    inaccessible ids sees the same "nothing valid here" signal that
    sending an empty list would produce.

    ATOMICITY
    ---------
    The service methods this view calls now wrap the update and the
    trust recompute in one atomic block — see
    `QuestionService.bulk_verify` / `bulk_unverify`. No change is
    needed here; the view already routes through the service.
    """
    permission_classes = [HasCapability]
    required_capability = 'questions.bulk_verify'
    throttle_classes = [BulkVerifyRateThrottle]

    def post(self, request):
        body = BulkVerifySerializer(data=request.data)
        if not body.is_valid():
            return api_error('بيانات غير صالحة', 400, details=body.errors)
        question_ids = body.validated_data['question_ids']
        if not question_ids:
            return api_error('لم يتم اختيار أي أسئلة', 400)
        if len(question_ids) > 500:
            return api_error('الحد الأقصى 500 سؤال في العملية الواحدة', 400)

        # Scope to visible questions (public + own drafts). The
        # resulting list is what actually reaches the service, so the
        # subsequent `_recompute_authors_after_verification` also only
        # sees questions the caller could legitimately act on.
        visible_ids = list(
            Question.objects
            .visible_to(request.user)
            .filter(id__in=question_ids)
            .values_list('id', flat=True)
        )
        if not visible_ids:
            return api_error('لم يتم العثور على أسئلة صالحة', 400)

        if body.validated_data['action'] == 'verify':
            count = QuestionService.bulk_verify(
                visible_ids,
                request.user.username,
                body.validated_data['verification_notes'],
            )
            msg = f'تم تدقيق {count} سؤال'
        else:
            count = QuestionService.bulk_unverify(visible_ids)
            msg = f'تم إلغاء تدقيق {count} سؤال'

        return api_success(data={'count': count}, message=msg)


class BulkTagUpdateView(APIView):
    """
    Add / remove tags across up to 500 questions in one call.
    Requires 'questions.bulk_tag'.

    ATOMICITY
    ---------
    `QuestionService.bulk_update_tags` now wraps the per-question
    M2M loop in one atomic block, so a failure partway through the
    batch rolls back the whole thing rather than leaving the first N
    questions modified and the rest untouched. No change is needed
    here; the view already routes through the service.
    """
    permission_classes = [HasCapability]
    required_capability = 'questions.bulk_tag'
    throttle_classes = [BulkVerifyRateThrottle]

    def post(self, request):
        body = BulkTagUpdateSerializer(data=request.data)
        if not body.is_valid():
            return api_error('بيانات غير صالحة', 400, details=body.errors)
        question_ids = body.validated_data['question_ids']
        if not question_ids:
            return api_error('لم يتم اختيار أي أسئلة', 400)
        if len(question_ids) > 500:
            return api_error('الحد الأقصى 500 سؤال في العملية الواحدة', 400)

        count = QuestionService.bulk_update_tags(
            question_ids,
            body.validated_data['add_tags'],
            body.validated_data['remove_tags'],
        )
        return api_success(data={'count': count}, message='تم تحديث الوسوم')


class UnverifiedListView(APIView):
    """
    Review queue source. Read-only.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        questions = (
            Question.objects.public()
            .filter(verified=False)
            .select_related('case', 'category', 'authored_by', 'owned_by')
            .prefetch_related('tags')
            .order_by('-created_at')
        )
        page, meta = paginate(questions, request)
        page_list = list(page)
        cache = QuestionSerializer.build_case_sibling_cache(page_list, request.user)
        serializer = QuestionSerializer(
            page_list, many=True,
            context={'request': request, 'case_sibling_cache': cache},
        )
        return api_success(data={'items': serializer.data, **meta})


class AvailableCountView(APIView):
    """
    Return how many questions match a prospective exam filter set.

    Query parameters (all optional):
        category_ids    comma-separated ints — multi-select
        category        single int — legacy, kept for old clients
        difficulty      easy / medium / hard
        verified_only   'true' to restrict to verified questions
        use_bookmarks   'true' to restrict to the caller's bookmarks
        tags_filter     comma-separated tag names
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        filters = {}

        category_ids_raw = request.query_params.get('category_ids')
        if category_ids_raw:
            parsed = parse_csv_param(category_ids_raw)
            if parsed:
                filters['category_ids'] = parsed
        elif 'category' in request.query_params:
            filters['category'] = request.query_params['category']

        if 'difficulty' in request.query_params:
            filters['difficulty'] = request.query_params['difficulty']
        if request.query_params.get('verified_only') == 'true':
            filters['verified'] = 'yes'
        if request.query_params.get('use_bookmarks') == 'true':
            filters['bookmark_user_id'] = request.user.id
        tags_filter = request.query_params.get('tags_filter')
        if tags_filter:
            parsed = parse_csv_param(tags_filter)
            if parsed:
                filters['tags_filter'] = parsed

        count = QuestionService.get_questions(filters, user=None).count()

        from django.conf import settings
        return api_success(data={
            'count': min(count, settings.MAX_QUIZ_QUESTIONS),
        })


class QuestionImageUploadView(APIView):
    """
    Upload or clear the image attached to a question.

    VISIBILITY SCOPING (fix — draft-existence oracle)
    -------------------------------------------------
    The lookup used to be a plain
    `get_object_or_404(Question, id=question_id)`. A member could
    probe `/questions/<id>/image/` and read the status code to
    distinguish "another author's draft exists" (403 from the
    ownership check) from "id does not exist" (404). The fetch now
    routes through `_fetch_question_for_action`, scoping the lookup
    to `visible_to(user)` unless the caller holds
    'questions.edit_any' — a holder can still reach any row.

    The 403 for "visible to you, but you lack the capability to
    modify" is preserved unchanged.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, question_id):
        # Scoped fetch: 404 for a draft the caller cannot see, same as
        # a nonexistent id. Callers holding 'questions.edit_any' still
        # reach any row.
        question = _fetch_question_for_action(
            request.user, question_id,
            any_cap='questions.edit_any',
        )

        if not _has_ownership_override(
            request.user, question,
            any_cap='questions.edit_any',
            own_cap='questions.upload_image',
        ):
            return api_error('غير مصرح لك بتعديل هذا السؤال', 403)

        wants_delete = (
            request.data.get('delete') in ('true', 'True', True)
            or ('image' in request.data and request.data.get('image') in ('', None))
        )
        if wants_delete:
            if question.image:
                question.image = None
                question.save(update_fields=['image'])
            return api_success(
                data={'image_url': None},
                message='تم حذف الصورة',
            )

        serializer = QuestionImageUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('الصورة غير صالحة', 400, details=serializer.errors)

        question.image = serializer.validated_data['image']
        question.save(update_fields=['image'])
        return api_success(
            data={'image_url': question.image.url if question.image else None},
            message='تم تحديث الصورة',
        )
