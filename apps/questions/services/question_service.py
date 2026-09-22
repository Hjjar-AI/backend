# backend/apps/questions/services/question_service.py

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from ..models import Question, Tag, ClinicalCase, clean_tag_name
from ..filters import filter_questions


class QuestionService:

    @staticmethod
    def _visible_qs(user=None):
        """
        Base queryset for every read path in this service.

        `select_related` list — one word change from the previous
        revision, and the reason is worth recording.

        The previous list was
            ('authored_by', 'owned_by', 'case')
        and did not include `category`. `QuestionSerializer` reads
        `category.name` and `category.color` on every row via
        `source='category.name'` / `source='category.color'`. Without
        the join, the main question list endpoint (QuestionListView,
        which routes through this method) fired one extra `SELECT
        FROM categories WHERE id = ?` per question on the page.

        Adding `category` here fixes that N+1 for every caller of
        `get_questions` and `get_question` in one place. The cost is
        one additional JOIN on a table with a handful of rows.
        """
        qs = (
            Question.objects.all()
            .select_related('authored_by', 'owned_by', 'case', 'category')
            .prefetch_related('tags')
        )
        if user is None or not getattr(user, 'is_authenticated', False):
            return qs.public()
        return qs.visible_to(user)

    @staticmethod
    def get_questions(filters=None, limit=None, offset=None, user=None):
        queryset = QuestionService._visible_qs(user)

        queryset = filter_questions(queryset, filters)

        queryset = queryset.order_by('-created_at')

        if offset is not None and limit is not None:
            queryset = queryset[offset:offset + limit]
        elif offset is not None:
            queryset = queryset[offset:]
        elif limit is not None:
            queryset = queryset[:limit]

        return queryset

    @staticmethod
    def get_question(question_id, user=None):
        return QuestionService._visible_qs(user).filter(id=question_id).first()

    # ── Author trust recompute ────────────────────────────────────────
    #
    # Both `bulk_verify` and `bulk_unverify` used to call
    # `user.update_trust_score()` in a Python loop over the affected
    # authors. That method runs two COUNT queries plus a save — three
    # round-trips per author. For a batch of 100 questions across 40
    # authors, that was 120 queries.
    #
    # `_recompute_author_trust` collapses the per-author COUNT work
    # into ONE aggregate query over every affected user id, then
    # issues a single `bulk_update` for the two fields that change.
    # The whole operation is bounded at three queries regardless of N:
    #
    #   1. Fetch the affected users' (id, username) pairs.
    #   2. Aggregate (total, verified) per authored_by_id in one pass.
    #   3. Bulk-update trust_score + questions_count.
    #
    # AUTHORSHIP RESOLUTION
    # ---------------------
    # Trust score is credited to `authored_by` — the field that
    # records who originally wrote the content. It is NOT `owned_by`.
    # A moderator who takes over someone else's questions should not
    # gain reputation for content they did not write, and the
    # original author should not lose credit when they hand off
    # maintenance. See the two-FK docstring on `Question` for the
    # full rationale.
    #
    # bulk_update is intentionally used instead of save() — it does
    # NOT fire signals, does NOT touch auto_now fields, and only
    # writes the two named columns. That is exactly the semantics we
    # want for a derived counter.
    @staticmethod
    def _recompute_author_trust(author_ids):
        from .author_reputation_service import AuthorReputationService
        AuthorReputationService.refresh_users(author_ids)

    # ── Verified/unverified transition helper ────────────────────────
    #
    # Both `bulk_verify` and `bulk_unverify` needed the same three
    # lines after their respective `queryset.update(...)` calls: pull
    # the distinct authored_by_id values for the affected questions,
    # then call `_recompute_author_trust` on that set. The two blocks
    # were byte-identical. Extracting them ensures the two verification
    # paths cannot drift on which authors get recomputed (a future
    # change like "also credit co-authors" would have had to land in
    # both, or it would have silently applied to only one direction).
    @staticmethod
    def _recompute_authors_after_verification(question_ids):
        author_ids = list(
            Question.objects
            .filter(id__in=question_ids, authored_by_id__isnull=False)
            .values_list('authored_by_id', flat=True)
            .distinct()
        )
        QuestionService._recompute_author_trust(author_ids)

    # ── CRUD ──────────────────────────────────────────────────────────

    @staticmethod
    def create_question(data, user):
        from ..serializers import QuestionCreateSerializer
        serializer = QuestionCreateSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            # Both FKs start pointing at the creator. They diverge only
            # when ownership is explicitly reassigned later (an admin
            # action not exposed on this path).
            #
            # The serializer's create() forwards these kwargs to
            # Question.objects.create(). It also reads `authored_by`
            # from validated_data to attribute the ClinicalCase, so the
            # case carries the same authorship as the question that
            # created it.
            question = serializer.save(
                authored_by=user,
                owned_by=user,
            )
            # The one place we still recompute inline. A user who
            # just created their first question sees their counter
            # increment immediately on the dashboard / profile
            # without waiting for a batch recalc. This is a single
            # author, so it is one call, not N.
            user.update_trust_score()
        return question

    @staticmethod
    def update_question(question_id, data, user):
        from ..serializers import QuestionUpdateSerializer, _resolve_case

        with transaction.atomic():
            instance = (
                Question.objects
                .select_related('case', 'authored_by', 'owned_by')
                .filter(id=question_id)
                .first()
            )
            if instance is None:
                raise Question.DoesNotExist(
                    f'Question {question_id} does not exist'
                )

            # The serializer validates and (for the case FK and tags)
            # mutates the instance in place. We do NOT call .save()
            # through the serializer for the version-bump path — the
            # optimistic-lock check and the version bump must be one
            # SQL statement (see below).
            #
            # NOTE ON AUTHORSHIP: `authored_by` and `owned_by` are not
            # in the serializer's Meta.fields, so they cannot be
            # modified through this path. DRF silently drops unknown
            # keys from the input. Reassigning ownership is a
            # dedicated admin action (see User.deactivate() and the
            # capability docs). `authored_by` is immutable by design.
            serializer = QuestionUpdateSerializer(
                instance, data=data, partial=True,
            )
            serializer.is_valid(raise_exception=True)
            validated = dict(serializer.validated_data)

            # ── Resolve and strip the m2m / FK write-only fields ──
            #
            # `expected_version` is read here, not from the raw
            # `data` dict. The field is declared on
            # `QuestionUpdateSerializer` (see
            # serializers/question_write.py) so that a malformed
            # value — "abc", a float, a negative number — is rejected
            # by DRF with a 400 before this method runs. Reading from
            # `data.get('expected_version')` bypassed that validation
            # entirely and let any string through to the CAS below,
            # where `int(expected_version)` would raise on
            # unparseable input and turn a 400 into a 500.
            tags_str = validated.pop('tags', None)
            case_key_present = 'case_key' in validated
            case_key = validated.pop('case_key', None)
            case_stem = validated.pop('case_stem', None)
            expected_version = validated.pop('expected_version', None)

            if case_key_present:
                instance.case = _resolve_case(
                    case_key, user,
                    stem=case_stem,
                    existing_case=instance.case,
                )

            update_fields = dict(validated)
            if case_key_present:
                update_fields['case'] = instance.case

            update_fields['updated_by'] = user.username
            update_fields['version'] = F('version') + 1
            update_fields['updated_at'] = timezone.now()

            if expected_version:
                rows = Question.objects.filter(
                    pk=question_id,
                    version=int(expected_version),
                ).update(**update_fields)
                if rows == 0:
                    raise ValueError("Question was modified by another user")
            else:
                Question.objects.filter(pk=question_id).update(**update_fields)

            instance.refresh_from_db()

            if tags_str is not None:
                instance.tags.clear()
                tags = [t.strip() for t in tags_str.split(',') if t.strip()]
                for tag_name in tags:
                    tag, _ = Tag.objects.get_or_create(
                        name=clean_tag_name(tag_name),
                    )
                    instance.tags.add(tag)

            # NOTE (perf): the previous version called
            # `instance.created_by_user.update_trust_score()` here.
            # That path recomputes the author's score on every
            # question edit — two COUNT queries plus a save. On an
            # edit-heavy install (a moderator fixing a batch of
            # typos) it was O(N) writes against the User table.
            #
            # The trust score is derived state; it is recomputed by
            # every path that legitimately changes it:
            #   • QuestionService.bulk_verify / bulk_unverify
            #   • QuestionService.create_question (immediate feedback)
            #   • QuestionService.toggle_verify (single verify)
            #   • AuthorReputationService.refresh_all()
            #     (management command + admin button)
            # Removing this call is safe: an edit that does not touch
            # `verified` (this serializer cannot — the field is not
            # writable here) does not change the score.

        return instance

    @staticmethod
    def delete_question(question_id):
        """
        Delete a question and refresh every affected author's cached
        reputation counters.

        REPUTATION RECOMPUTE (fix — issue: stored counters drift)
        ---------------------------------------------------------
        `AuthorReputationService.refresh_users` derives
        `questions_count` and `trust_score` from the set of PUBLIC
        (`is_draft=False`) questions where `authored_by` is this user.
        Deleting a question removes a row from that set, so every
        author whose count changes must be recomputed.

        Before this revision the recompute was deliberately skipped —
        the docstring argued it was a "rare enough" operation to let
        the counter drift until the next batch refresh. That left the
        user's profile, dashboard, and author-rank badge wrong until
        someone happened to run `manage.py refresh_author_ranks` (or
        touched the admin refresh button). On a review workflow where
        deletions are routine, the drift was permanent in practice.

        The recompute now runs here, bounded at one aggregate query
        over the small set of authors that actually lost a question
        (usually exactly one), plus one bulk update.

        The author id is captured BEFORE the delete, because the
        `authored_by` FK on the deleted row is gone by the time the
        recompute runs.
        """
        question = Question.objects.get(id=question_id)

        affected_author_ids = []
        if question.authored_by_id is not None and not question.is_draft:
            # A draft never contributed to the counter, so deleting
            # one cannot change it — skip the recompute in that case.
            affected_author_ids.append(question.authored_by_id)

        with transaction.atomic():
            question.delete()

        if affected_author_ids:
            QuestionService._recompute_author_trust(affected_author_ids)

    # ── Single-question verification (fix — ToggleVerifyView) ─────────
    #
    # `ToggleVerifyView.post` used to hand-roll the "flip verified,
    # recompute the author's trust score" sequence directly in the
    # view, with no `transaction.atomic()` around the two writes and
    # no call into any service method. That was the third independent
    # implementation of the same operation in the codebase (after
    # `create_question` and `bulk_verify`), and the only one missing
    # both the transaction and the shared `_recompute_author_trust`
    # helper.
    #
    # Consequence of the missing transaction: if the process died or
    # `update_trust_score()` raised between `question.save()` and the
    # recompute, the question was left marked verified with no
    # trust-score credit. Worse, because the view TOGGLES rather than
    # sets, a client retry would flip the flag back — the intermediate
    # state was not only inconsistent but self-correcting in the wrong
    # direction.
    #
    # The method below mirrors `create_question` exactly: one atomic
    # block, both writes inside it. The view now calls this method
    # instead of duplicating the logic.

    @staticmethod
    def toggle_verify(question, user):
        """
        Flip `verified` on one question and recompute the author's
        trust counters, atomically.

        The caller is responsible for the visibility and capability
        gates — this method assumes `question` is a row the caller is
        permitted to act on. It performs only the write itself.

        Returns the new `verified` value.
        """
        # Capture the author id BEFORE the flip, so the recompute
        # below uses the same row the write just touched. `authored_by`
        # is not modified here, but capturing early keeps the recompute
        # call symmetric with the rest of the service layer (compare
        # `delete_question`).
        affected_author_id = question.authored_by_id

        with transaction.atomic():
            if question.verified:
                question.verified = False
                question.verified_by = None
                question.verified_at = None
                question.verification_notes = None
            else:
                question.verified = True
                question.verified_by = user.username
                question.verified_at = timezone.now()
            question.save()

            # Credit the change to the author, not the owner or the
            # verifier. Verification changes the author's trust score,
            # regardless of who happens to steward the question today.
            if affected_author_id is not None:
                QuestionService._recompute_author_trust([affected_author_id])

        return question.verified

    # ── Batch verification (fix — missing atomic around both writes) ──
    #
    # Both methods below used to run `queryset.update(...)` and then
    # `_recompute_authors_after_verification(...)` as two independent
    # statements with no transaction between them. If the second
    # failed (a DB hiccup on the bulk_update against the User table,
    # a timeout on the aggregate), the verified flags were committed
    # but the stored trust counters were not — the exact inconsistency
    # `create_question` already guards against, and the exact shape
    # the shared `_recompute_authors_after_verification` helper was
    # extracted to prevent.
    #
    # Both are wrapped now. The recompute reads the just-updated rows
    # inside the same transaction, so the aggregate sees the new
    # `verified` values.

    @staticmethod
    def bulk_verify(question_ids, verifier, notes):
        with transaction.atomic():
            count = Question.objects.filter(id__in=question_ids).update(
                verified=True,
                verified_by=verifier,
                verified_at=timezone.now(),
                verification_notes=notes,
            )
            # Recompute via the shared helper — same shape that
            # bulk_unverify below needs. See
            # `_recompute_authors_after_verification`.
            QuestionService._recompute_authors_after_verification(question_ids)
        return count

    @staticmethod
    def bulk_unverify(question_ids):
        with transaction.atomic():
            count = Question.objects.filter(id__in=question_ids).update(
                verified=False,
                verified_by=None,
                verified_at=None,
                verification_notes=None,
            )
            QuestionService._recompute_authors_after_verification(question_ids)
        return count

    # ── Batch tag update (fix — missing atomic across N M2M writes) ──
    #
    # The per-question loop below performs two M2M operations per
    # question (add and remove), each of which touches the through
    # table. For a batch of 100 questions that is up to 200 separate
    # writes. Without a transaction, a failure on question 50 left
    # questions 1-49 modified and 50-100 untouched — a partially
    # applied batch with no rollback and no signal to the caller,
    # because `count` is only returned on full success.
    #
    # The method does NOT trigger a trust recompute — tag changes do
    # not affect `verified` status, and `AuthorReputationService`
    # aggregates on `authored_by` + `is_draft` + `verified`, none of
    # which a tag change alters.

    @staticmethod
    def bulk_update_tags(question_ids, add_tags, remove_tags):

        questions = list(Question.objects.filter(id__in=question_ids))

        # Resolve add-tags once. Deduplicate on the cleaned name so a
        # caller that sends "  schizophrenia " and "schizophrenia"
        # does not create a duplicate get_or_create on the second
        # entry.
        add_tag_objects = []
        seen_add = set()
        for raw in add_tags:
            name = clean_tag_name(raw) if raw is not None else ''
            if not name or name in seen_add:
                continue
            seen_add.add(name)
            tag, _ = Tag.objects.get_or_create(name=name)
            add_tag_objects.append(tag)

        # Resolve remove-tags once. No cleaning — matches the previous
        # `.filter(name=tag_name).first()` lookup. Non-string entries
        # are skipped (defensive — see the docstring).
        remove_names = set()
        for raw in remove_tags:
            if not isinstance(raw, str):
                continue
            name = raw.strip()
            if name:
                remove_names.add(name)
        remove_tag_objects = (
            list(Tag.objects.filter(name__in=remove_names))
            if remove_names else []
        )

        with transaction.atomic():
            count = 0
            for q in questions:
                if add_tag_objects:
                    q.tags.add(*add_tag_objects)
                if remove_tag_objects:
                    q.tags.remove(*remove_tag_objects)
                count += 1
        return count

    # ── Question duplication (fix — QuestionDuplicateView) ────────────
    #
    # `QuestionDuplicateView.post` used to hand-roll the copy directly
    # in the view: `Question.objects.create(...)`, then
    # `new_q.tags.set(original.tags.all())` (a separate M2M write
    # against the through table), then `request.user.update_trust_score()`
    # — three writes across two tables with no `transaction.atomic()`
    # anywhere in the method.
    #
    # Same failure shape as the `ToggleVerifyView` gap above, but with
    # one more write in the chain: a crash between `create()` and
    # `tags.set()` produced a copy with no tags; a crash between
    # `tags.set()` and `update_trust_score()` produced a copy whose
    # author's `questions_count` had not been incremented. On retry,
    # the client would create a SECOND copy, doubling the orphaned
    # state rather than healing it.
    #
    # Extracted into the service layer (rather than just wrapped in
    # the view) so this write path matches `create_question`,
    # `update_question`, `delete_question`, `toggle_verify`, and
    # `bulk_verify` — every other question-write operation in the
    # codebase lives in this module.

    @staticmethod
    def duplicate_question(original, user):
        """
        Create a copy of `original` attributed to and owned by `user`,
        atomically.

        Behavior preserved from the previous view implementation:

          • The copy starts `verified=False`. A duplicate must be
            independently verified; the original's verification
            state is not inherited.
          • Draft state is inherited: a duplicate of a public
            question is public; a duplicate of a draft the caller
            owns is a draft owned by the caller. Copying a private
            draft into the public bank would be a surprising way to
            leak it — even the caller's own drafts.
          • Case linkage is preserved, with `case_order` set to
            one past the current maximum within the case.
          • The `image` FileField pointer is copied, not the file.
            Both rows reference the same file on disk. (This is
            pre-existing behavior; changing it would require a real
            file copy and is out of scope for the atomicity fix.)
          • Tags are copied.
          • The caller's trust score is recomputed, because the
            duplicate has `authored_by=user` and their authored set
            just grew by one.

        Returns the new Question instance.

        The caller is responsible for the `questions.duplicate`
        capability check and for fetching `original` through a
        visibility-scoped queryset — see `QuestionDuplicateView.post`.
        """
        # Draft-state inheritance. See the docstring.
        is_draft = bool(original.is_draft)
        draft_owner = user if is_draft else None
            # Case-order assignment: one past the current maximum
            # within the case. This does NOT guarantee monotonicity
            # under concurrent duplicates of the same case — the
            # `Question` model has no unique constraint on
            # `(case_id, case_order)`, so two racing transactions can
            # each compute the same "one past max" and both insert
            # with that value. The read is inside the atomic block
            # only because the whole copy operation is, not because
            # it defends against that race. A monotonic guarantee
            # would require either a `SELECT ... FOR UPDATE` on the
            # case row or a unique constraint on
            # `(case_id, case_order)`; both are out of scope for the
            # atomicity fix this method exists to deliver.
        with transaction.atomic():
            new_case_order = None
            if original.case_id is not None:
                from django.db.models import Max
                current_max = (
                    Question.objects
                    .filter(case_id=original.case_id)
                    .aggregate(m=Max('case_order'))['m']
                )
                new_case_order = (current_max or 0) + 1

            new_q = Question.objects.create(
                question=original.question,
                choices=original.choices,
                correct_answer=original.correct_answer,
                explanation=original.explanation,
                source=original.source,
                difficulty=original.difficulty,
                category=original.category,
                verified=False,
                authored_by=user,
                owned_by=user,
                image=original.image,
                case=original.case,
                case_order=new_case_order,
                is_draft=is_draft,
                draft_owner=draft_owner,
            )
            new_q.tags.set(original.tags.all())
            user.update_trust_score()

        return new_q

    # ═══════════════════════════════════════════════════════════════════
    # FEATURE (case-based question chains) — case edit helper
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def update_case_stem(case_key, new_stem, user):
        """
        Replace the shared clinical vignette on a case.

        AUTHORSHIP CHECK
        ----------------
        The 'edit_case_stem_own' branch tests authorship via the
        `authored_by` FK on the case's questions. This is the same
        FK that drives reputation; the case-edit permission and the
        reputation attribution both follow "who wrote it", which is
        the correct semantic for editing the vignette itself (as
        opposed to reassigning ownership, which is a separate action).

        RETURN VALUE — SCOPED COUNT (this revision)
        -------------------------------------------
        The return value is the count of questions whose stem was
        changed *as far as the caller can see*. It used to be
        `case.questions.count()` — the total across every author.

        Two callers consume the return value:

          • `CaseStemUpdateView.post` renders it into the success
            message (`تم تحديث نص الحالة في N سؤال`).
          • `CaseDetailView.put` writes it into the audit row's
            `updated_count` field.

        Both reach this method with `request.user`. A caller who can
        edit the case (via `edit_case_stem_any`, or via
        `edit_case_stem_own` plus case authorship) but who does not
        hold `questions.edit_any` used to learn, from the number
        alone, how many other authors' private drafts were attached.

        TWO-TIER SCOPING — WHY `edit_any`
        ---------------------------------
        `questions.edit_any` is the codebase's "see every question,
        including other authors' private drafts" flag — see
        `_fetch_question_for_action` in
        `apps/questions/views/question_views.py`. It is the correct
        bypass here for the same reason.

        `edit_case_stem_any` is deliberately NOT used as the bypass
        even though it is what gates the case-edit operation itself.
        The two capabilities are coupled in the default role map
        (moderators and admins hold both), so today the distinction
        makes no difference — but a deployment that split the two
        would silently re-open the leak if the scoping keyed on the
        wrong one.

        `case.questions` is not used here even for the `edit_any`
        path, because the related-manager form ignores any future
        change to `visible_to`. The scoped form below follows the
        queryset's rule verbatim.
        """
        if not case_key:
            return 0

        case = ClinicalCase.objects.filter(key=case_key).first()
        if case is None:
            return 0

        from apps.questions.case_policy import can_edit_case
        if not can_edit_case(user, case):
            raise PermissionError('CASE_STEM_NOT_AUTHOR')

        if new_stem is None:
            normalized = None
        else:
            normalized = str(new_stem).strip() or None

        case.stem = normalized
        case.save(update_fields=['stem', 'updated_at'])

        qs = Question.objects.all()
        if not user.has_capability('questions.edit_any'):
            qs = qs.visible_to(user)
        return qs.filter(case_id=case.id).count()