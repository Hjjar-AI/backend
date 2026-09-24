# backend/apps/learning/srs_service.py
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from .models import UserQuestionAttempt
from .confidence import normalize_confidence, is_confident as confidence_is_high
from apps.questions.models import Question

logger = logging.getLogger(__name__)

EASE_FLOOR = 1.3
EASE_DEFAULT = 2.5


def _next_interval(repetitions, previous_interval, ease):
    if repetitions <= 1:
        return 1
    if repetitions == 2:
        return 6
    return max(1, round(previous_interval * ease))


def _apply_review(
    attempt,
    is_correct,
    confidence_score=None,
    error_reason=None,
    now=None,
    is_confident=None,
):
    # ``is_confident`` is the pre-score keyword retained for callers and
    # older integrations that still pass a boolean. New code should pass
    # ``confidence_score`` (1=guessing, 2=uncertain, 3=confident).
    if confidence_score is None and is_confident is not None:
        confidence_score = is_confident
    now = now or timezone.now()
    confidence_score = normalize_confidence(confidence_score)
    confident = confidence_is_high(confidence_score)

    attempt.attempts = (attempt.attempts or 0) + 1
    attempt.last_correct = is_correct
    attempt.last_confidence = confident
    attempt.last_confidence_score = confidence_score
    attempt.last_error_reason = error_reason if not is_correct else None
    attempt.last_answered_at = now

    if is_correct:
        attempt.ever_correct = True
    else:
        attempt.wrong_count = (attempt.wrong_count or 0) + 1

    if is_correct and confident:
        quality = 5
        policy = 'standard'
    elif is_correct and not confident:
        quality = 3
        policy = 'standard'
    else:
        reason = error_reason or 'unknown'
        if reason == 'misread':
            quality = 3
            policy = 'misread'
        elif reason == 'confused':
            quality = 3
            policy = 'confused'
        elif reason == 'guessed':
            quality = 2
            policy = 'guessed'
        else:
            quality = 1
            policy = 'unknown'

    # SM-2 ease adjustment. Applied once, regardless of policy.
    ease = attempt.ease_factor or EASE_DEFAULT
    ease = ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    attempt.ease_factor = max(EASE_FLOOR, round(ease, 3))

    if policy == 'standard':
        attempt.repetitions = (attempt.repetitions or 0) + 1
        attempt.interval_days = _next_interval(
            attempt.repetitions,
            attempt.interval_days or 0,
            attempt.ease_factor,
        )
    elif policy == 'misread':
        attempt.interval_days = 2
    elif policy == 'confused':
        attempt.repetitions = max(0, (attempt.repetitions or 0) - 1)
        attempt.interval_days = 3
    elif policy == 'guessed':
        attempt.repetitions = 0
        attempt.interval_days = 1
    else:  # 'unknown'
        attempt.repetitions = 0
        attempt.interval_days = 1

    attempt.next_due = now + timedelta(days=attempt.interval_days)
    return attempt


class SRSService:

    @staticmethod
    def _visible_attempts(user):
        return UserQuestionAttempt.objects.filter(
            user=user,
            question_id__in=Question.objects.visible_to(user).values_list('id', flat=True),
        )

    # The set of fields _apply_review mutates. Kept as a class
    # attribute so the three writers below (bulk_update, the
    # per-row update_or_create fallback, and any future caller)
    # cannot drift out of agreement.
    _REVIEW_FIELDS = (
        'attempts', 'wrong_count', 'ever_correct',
        'last_correct', 'last_confidence', 'last_error_reason',
        'last_confidence_score',
        'last_answered_at',
        'ease_factor', 'interval_days', 'repetitions', 'next_due',
    )

    # ── Due-filter helper — single source of truth ────────────────
    #
    # The "is this attempt due for review" predicate — the OR of
    # `next_due <= now` and `next_due IS NULL` — was hand-written in
    # both `due_question_ids` and `due_count`, and a third copy was
    # implied by the extra query `attempt_summary` fired just to
    # recompute the same set. `_apply_due_filter` is now the ONE
    # place the predicate lives. Any future change (e.g. adding a
    # "skip attempts answered in the last hour" clause) lands once.
    #
    # `attempt_summary` calls `_apply_due_filter(qs)` on the queryset
    # it already built, instead of re-deriving the queryset through
    # `due_count(user)` and firing a second aggregate.
    @staticmethod
    def _apply_due_filter(qs, now=None):
        now = now or timezone.now()
        return qs.filter(Q(next_due__lte=now) | Q(next_due__isnull=True))

    @staticmethod
    def _due_qs(user, now=None):
        """Visible attempts that are due for review right now."""
        return SRSService._apply_due_filter(
            SRSService._visible_attempts(user), now=now,
        )

    @staticmethod
    @transaction.atomic
    def record_attempts_bulk(user, results):
        """
        Persist one review per answered question.

        LIVE-QUESTION FILTER (fix — deleted-mid-session crash)
        ------------------------------------------------------
        A session graded from a frozen snapshot can legitimately
        contain a question whose `Question` row has since been
        deleted — the snapshot is self-sufficient for GRADING, but
        `UserQuestionAttempt.question_id` is a non-null FK to the
        live row, and inserting a dangling id raises IntegrityError
        inside the finish transaction.
        """
        if not results:
            return 0

        answered = [r for r in results if r.get('user_answer') is not None]
        if not answered:
            return 0

        requested_qids = [
            r['question_id'] for r in answered if r.get('question_id')
        ]
        if not requested_qids:
            return 0

        live_qids = set(
            Question.objects
            .filter(id__in=requested_qids)
            .values_list('id', flat=True)
        )
        answered = [r for r in answered if r['question_id'] in live_qids]
        if not answered:
            return 0

        qids = [r['question_id'] for r in answered]
        existing = {
            a.question_id: a
            for a in UserQuestionAttempt.objects.filter(user=user, question_id__in=qids)
        }

        now = timezone.now()
        to_create = []
        to_update = []

        for r in answered:
            qid = r['question_id']
            is_correct = bool(r.get('is_correct'))
            confidence_score = normalize_confidence(
                r.get('confidence_score', r.get('confidence', 3)),
            )
            error_reason = r.get('error_reason')

            attempt = existing.get(qid)
            if attempt is None:
                attempt = UserQuestionAttempt(user=user, question_id=qid)

            _apply_review(
                attempt,
                is_correct,
                confidence_score,
                error_reason=error_reason,
                now=now,
            )

            if attempt.pk is None:
                to_create.append(attempt)
            else:
                to_update.append(attempt)

        if to_create:
            try:
                with transaction.atomic():
                    UserQuestionAttempt.objects.bulk_create(to_create)
            except IntegrityError:
                logger.warning(
                    'SRSService.record_attempts_bulk: bulk_create hit '
                    'a unique constraint (user_id=%s, question_ids=%s) '
                    '— a concurrent finish inserted the same '
                    '(user, question) pairs. Retrying via per-row '
                    'update_or_create so this transaction\'s _apply_review '
                    'state wins.',
                    user.id, [a.question_id for a in to_create],
                )
                for attempt in to_create:
                    UserQuestionAttempt.objects.update_or_create(
                        user=user,
                        question_id=attempt.question_id,
                        defaults={
                            field: getattr(attempt, field)
                            for field in SRSService._REVIEW_FIELDS
                        },
                    )

        if to_update:
            UserQuestionAttempt.objects.bulk_update(
                to_update,
                list(SRSService._REVIEW_FIELDS),
            )

        return len(answered)

    @staticmethod
    def due_question_ids(user, limit=None):
        qs = (
            SRSService._due_qs(user)
            .order_by('next_due', 'last_answered_at')
            .values_list('question_id', flat=True)
        )
        if limit is not None:
            qs = qs[:limit]
        return list(qs)

    @staticmethod
    def due_count(user):
        return SRSService._due_qs(user).count()

    @staticmethod
    def wrong_question_ids(user):
        return list(
            SRSService._visible_attempts(user)
            .filter(ever_correct=False)
            .order_by('-wrong_count', '-last_answered_at')
            .values_list('question_id', flat=True)
        )

    @staticmethod
    def fragile_question_ids(user):
        return list(
            SRSService._visible_attempts(user)
            .filter(
                last_correct=True,
                last_confidence=False,
            )
            .order_by('-last_answered_at')
            .values_list('question_id', flat=True)
        )

    @staticmethod
    def attempt_summary(user):
        """
        One-query summary of the caller's SRS state.

        PERF FIX
        --------
        The previous implementation did:

            qs = SRSService._visible_attempts(user)
            total = qs.count()
            return {
                'total_seen': total,
                'ever_correct': qs.filter(ever_correct=True).count(),
                'wrong_open':   qs.filter(ever_correct=False).count(),
                'fragile_correct': qs.filter(...).count(),
                'due_now':      SRSService.due_count(user),  # <- new
                                                              #    _visible_attempts
                                                              #    subquery
            }

        That was five aggregate queries, one of which — `due_count`
        — rebuilt `_visible_attempts(user)` (itself a subquery over
        `Question.objects.visible_to(user)`) from scratch instead of
        reusing the `qs` the caller already had.

        The whole thing is now ONE aggregate query with five
        conditional counts. The `_visible_attempts` subquery fires
        exactly once per call to this method.

        ALIAS RENAME (fix — FieldError)
        -------------------------------
        The alias for the "answered correctly at least once" count
        is `ever_correct_total`, NOT `ever_correct`. The name
        `ever_correct` is the underlying BooleanField that the
        aggregate's own filter tests, and Django resolves a filter
        lookup against the query's annotation registry before the
        model's field namespace. The aggregate alias is registered
        into that registry during the same `.aggregate()` call, so
        a filter of the form `Q(ever_correct=True)` was resolving
        to the aggregate itself instead of to the field, and Django
        raised

            FieldError: Cannot compute Count('ever_correct'):
            'ever_correct' is an aggregate

        The response dict below still emits the wire key
        `ever_correct` (the key every caller reads), so no
        client-side change is needed. Only the intermediate alias
        is different.

        The rule this fix encodes: do NOT name an aggregate alias
        after a field the aggregate's own filter (or the filter of
        a sibling aggregate in the same call) references. Name it
        after the metric it measures — `ever_correct_total`,
        `wrong_open`, `fragile_correct`, `due_now` — and map to the
        wire key in the return dict.
        """
        qs = SRSService._visible_attempts(user)
        now = timezone.now()

        stats = qs.aggregate(
            total_seen=Count('id'),
            ever_correct_total=Count(
                'id', filter=Q(ever_correct=True),
            ),
            wrong_open=Count(
                'id', filter=Q(ever_correct=False),
            ),
            fragile_correct=Count(
                'id',
                filter=Q(last_correct=True, last_confidence=False),
            ),
            due_now=Count(
                'id',
                filter=Q(next_due__lte=now) | Q(next_due__isnull=True),
            ),
        )

        return {
            'total_seen': stats['total_seen'] or 0,
            'ever_correct': stats['ever_correct_total'] or 0,
            'wrong_open': stats['wrong_open'] or 0,
            'fragile_correct': stats['fragile_correct'] or 0,
            'due_now': stats['due_now'] or 0,
        }
