# backend/apps/analytics/services/member.py
"""
Member-facing analytics — the original methods.

Every function here is a self-contained query builder against models
in users / questions / learning / exams. No function calls another.

Dropped imports (present in the original but never referenced):
  • QuestionTag     — from apps.questions.models
  • TruncMonth      — from django.db.models.functions
  • QuestionFlag    — from apps.feedback.models
  • Group,
    GroupMembership — from apps.groups.models

All four were dead in the original module and are correctly absent
here. Nothing in this module needs them.

MARIADB TIMEZONE-TABLES CAVEAT (the `TruncDate` removals below)
---------------------------------------------------------------
Two functions in this module used to annotate with
`TruncDate('completed_at')` / `TruncDate('last_login')` /
`TruncDate('created_at')`. On MariaDB that compiles to

    DATE(CONVERT_TZ(column, 'UTC', 'Asia/Damascus'))

which requires the `mysql.time_zone*` tables to be populated. On a
stock Debian/Ubuntu MariaDB install those tables are empty, so
`CONVERT_TZ` returns NULL, Django's converter rejects the NULL
result, and the request 500s with

    ValueError: Database returned an invalid datetime value.
    Are time zone definitions for your database installed?

The correct long-term fix is to load the tables once per server:

    sudo mysql_tzinfo_to_sql /usr/share/zoneinfo | sudo mysql mysql

Until that is guaranteed on every deployment, the two affected
functions group by local calendar day in Python. This matches the
same decision already made for `VerificationStatsView` in
`apps/analytics/views.py` and for `get_weekly_retention` in
`apps/analytics/services/admin_advanced.py`. The response shapes
are unchanged, so no frontend change is involved.
"""

from datetime import timedelta

from django.db.models import (
    Avg, Case, Count, F, FloatField, Q, Sum, When,
)
from django.utils import timezone

from apps.users.models import User
from apps.questions.models import Question, Category, Tag
from apps.learning.models import UserQuestionAttempt
from apps.exams.models import TestHistory


def get_category_coverage():
    results = (
        Category.objects.annotate(
            total=Count('questions', filter=Q(questions__is_draft=False), distinct=True),
            verified=Count(
                'questions',
                filter=Q(questions__verified=True, questions__is_draft=False),
                distinct=True,
            ),
            avg_accuracy=Avg(
                Case(
                    When(
                        questions__times_answered__gt=0,
                        questions__is_draft=False,
                        then=1.0 * F('questions__times_correct') / F('questions__times_answered'),
                    ),
                    default=None,
                    output_field=FloatField(),
                )
            ),
        )
        .values('id', 'name', 'color', 'total', 'verified', 'avg_accuracy')
    )
    return [
        {
            'category_id': r['id'],
            'category_name': r['name'],
            'total': r['total'] or 0,
            'verified': r['verified'] or 0,
            'avg_accuracy': r['avg_accuracy'] or 0.0,
            'color': r['color'],
        }
        for r in results
    ]


def get_tag_coverage():
    results = (
        Tag.objects.annotate(
            count=Count(
                'questiontag',
                filter=Q(questiontag__question__is_draft=False),
                distinct=True,
            ),
            verified_count=Count(
                'questiontag__question',
                filter=Q(
                    questiontag__question__verified=True,
                    questiontag__question__is_draft=False,
                ),
                distinct=True,
            ),
        )
        .values('name', 'count', 'verified_count')
    )
    return [
        {
            'tag_name': r['name'],
            'count': r['count'] or 0,
            'verified_count': r['verified_count'] or 0,
        }
        for r in results
    ]


def get_difficulty_stats():
    results = (
        Question.objects.public().values('difficulty')
        .annotate(
            count=Count('id'),
            avg_correct_rate=Avg(
                Case(
                    When(times_answered__gt=0, then=1.0 * F('times_correct') / F('times_answered')),
                    default=None,
                    output_field=FloatField(),
                )
            ),
        )
        .order_by('difficulty')
    )
    return [
        {
            'difficulty': r['difficulty'],
            'count': r['count'],
            'avg_correct_rate': r['avg_correct_rate'] or 0.0,
        }
        for r in results
    ]


def get_user_performance_trend(user_id, days=30):
    """
    Per-day accuracy and answered-question count for one user.

    PYTHON-SIDE DAY GROUPING (MariaDB timezone-tables fix)
    -------------------------------------------------------
    The previous implementation used `TruncDate('completed_at')`,
    which on MariaDB compiles to

        DATE(CONVERT_TZ(completed_at, 'UTC', 'Asia/Damascus'))

    and depends on the `mysql.time_zone*` tables being populated.
    On a stock Debian/Ubuntu MariaDB install those tables are
    empty, so `CONVERT_TZ` returns NULL and Django raises

        ValueError: Database returned an invalid datetime value.
        Are time zone definitions for your database installed?

    The correct long-term fix is to load the tables once per server:

        sudo mysql_tzinfo_to_sql /usr/share/zoneinfo | sudo mysql mysql

    Until that is done on every deployment, this function groups by
    local calendar day in Python. The response shape is unchanged.

    QUERY SHAPE
    -----------
    One range scan over `(user_id, completed_at)` reading three
    columns — `completed_at`, `accuracy`, `total_questions` — and
    ordered by `completed_at`. The order matters only for
    reproducibility of the floating-point accumulation below; the
    grouping itself does not depend on row order.

    ACCURACY AGGREGATION
    --------------------
    The original used `Avg('accuracy')` at the DB layer, which is
    `SUM(accuracy) / COUNT(*)` over the day's sessions. This
    version computes the same quantity by accumulating a running
    sum and a count per day. Floating-point order of operations
    differs slightly from what MariaDB's `AVG()` would produce —
    SQL aggregates in the engine's internal row order, this
    version aggregates in the query's `ORDER BY` order — so the
    last decimal digit of a day's average may differ in rare
    cases. For a display value this is irrelevant; if exact
    bit-parity with the SQL path were ever required, the sum
    could be accumulated as `decimal.Decimal` or
    `fractions.Fraction` instead of `float`.
    """
    cutoff = timezone.now() - timedelta(days=days)

    rows = (
        TestHistory.objects
        .filter(user_id=user_id, completed_at__gte=cutoff)
        .values_list('completed_at', 'accuracy', 'total_questions')
        .order_by('completed_at')
    )

    # day (a datetime.date) → {'accuracy_sum': float, 'count': int,
    #                          'questions': int}
    #
    # The key is a `datetime.date` (not a string) so the final sort
    # is a plain chronological sort, and `.isoformat()` is applied
    # once when building the response.
    buckets = {}
    for completed_at, accuracy, total_questions in rows:
        # `completed_at` is non-nullable on TestHistory; the guard
        # is defensive against a legacy row that somehow escaped
        # the NOT NULL constraint before it was introduced.
        if completed_at is None:
            continue
        day = timezone.localtime(completed_at).date()
        bucket = buckets.setdefault(
            day, {'accuracy_sum': 0.0, 'count': 0, 'questions': 0},
        )
        bucket['accuracy_sum'] += accuracy or 0.0
        bucket['count'] += 1
        bucket['questions'] += total_questions or 0

    return [
        {
            'date': day.isoformat(),
            'accuracy': buckets[day]['accuracy_sum'] / buckets[day]['count'],
            'questions_answered': buckets[day]['questions'],
        }
        for day in sorted(buckets.keys())
    ]


def get_active_users_stats(days=30):
    """
    Per-day counts of active users, new users, and sessions.

    PYTHON-SIDE DAY GROUPING (MariaDB timezone-tables fix)
    -------------------------------------------------------
    The previous implementation used three `TruncDate` annotations
    — one on `User.last_login`, one on `User.created_at`, and one
    on `TestHistory.completed_at` — each of which on MariaDB
    compiles to a `CONVERT_TZ(...)` call dependent on the
    `mysql.time_zone*` tables. See `get_user_performance_trend`
    above for the full rationale; the fix is the same.

    QUERY SHAPE
    -----------
    Three independent column reads, one per series, each a range
    scan on the respective timestamp column:

      • `User.last_login`     filtered on `last_login__gte=cutoff`
      • `User.created_at`     filtered on `created_at__gte=cutoff`
      • `TestHistory.completed_at` filtered on `completed_at__gte=cutoff`

    No `GROUP BY` in SQL. The three series are merged into a single
    `{date: entry}` map in Python so the response carries one entry
    per day that has data in any of the three.

    RESPONSE SHAPE
    --------------
    Unchanged: `[{date, active_users, new_users, total_sessions,
    avg_session_duration}, ...]` sorted by ascending date.

    AVERAGE DURATION
    ----------------
    `avg_session_duration` is the mean `time_spent` across the
    day's `TestHistory` rows, matching the original
    `Avg('time_spent')`. Durations are accumulated in a separate
    `(sum, count)` accumulator rather than on the output dict so
    the final response never carries transient helper keys.
    """
    cutoff = timezone.now() - timedelta(days=days)

    data_by_date = {}

    def _bucket(day):
        entry = data_by_date.get(day)
        if entry is None:
            entry = {
                'date': day.isoformat(),
                'active_users': 0,
                'new_users': 0,
                'total_sessions': 0,
                'avg_session_duration': 0.0,
            }
            data_by_date[day] = entry
        return entry

    # ── Active users — bucket each user's last_login by local day ──
    #
    # A user has exactly one `last_login` value, so each row in the
    # iteration below contributes one to the bucket for its local
    # day. This matches the original `Count('id')` grouped by
    # `TruncDate('last_login')` — that query also produced one
    # count per user per day.
    for (last_login,) in (
        User.objects
        .filter(last_login__gte=cutoff)
        .values_list('last_login')
    ):
        if last_login is None:
            continue
        _bucket(timezone.localtime(last_login).date())['active_users'] += 1

    # ── New users — bucket each user's created_at by local day ────
    for (created_at,) in (
        User.objects
        .filter(created_at__gte=cutoff)
        .values_list('created_at')
    ):
        # `created_at` is non-nullable; the guard is defensive.
        if created_at is None:
            continue
        _bucket(timezone.localtime(created_at).date())['new_users'] += 1

    # ── Sessions — bucket completed_at and accumulate durations ───
    #
    # `sessions_acc` maps a day to `(duration_sum, session_count)`.
    # The final `avg_session_duration` is computed in the pass
    # below, once every session has been seen.
    sessions_acc = {}
    for completed_at, time_spent in (
        TestHistory.objects
        .filter(completed_at__gte=cutoff)
        .values_list('completed_at', 'time_spent')
    ):
        if completed_at is None:
            continue
        day = timezone.localtime(completed_at).date()
        _bucket(day)['total_sessions'] += 1
        prev_sum, prev_count = sessions_acc.get(day, (0, 0))
        sessions_acc[day] = (prev_sum + (time_spent or 0), prev_count + 1)

    # ── Finalize averages ─────────────────────────────────────────
    #
    # Every key in `sessions_acc` is also a key in `data_by_date`
    # (because `_bucket(day)` was called before the accumulator was
    # updated), so the dict lookup below cannot miss.
    for day, (duration_sum, count) in sessions_acc.items():
        if count:
            data_by_date[day]['avg_session_duration'] = duration_sum / count

    return [data_by_date[day] for day in sorted(data_by_date.keys())]


def get_user_weak_categories(user_id, min_attempts=3, top=3):
    rows = (
        UserQuestionAttempt.objects
        .filter(user_id=user_id, question__category__isnull=False)
        .values('question__category_id', 'question__category__name')
        .annotate(
            attempts=Sum('attempts'),
            wrongs=Sum('wrong_count'),
        )
    )
    ranked = []
    for r in rows:
        att = r['attempts'] or 0
        if att < min_attempts:
            continue
        wrong = r['wrongs'] or 0
        accuracy = ((att - wrong) / att) * 100 if att else 0
        ranked.append({
            'category_id': r['question__category_id'],
            'category_name': r['question__category__name'],
            'attempts': att,
            'wrong_count': wrong,
            'accuracy': round(accuracy, 1),
        })
    ranked.sort(key=lambda x: (x['accuracy'], -x['attempts']))
    return ranked[:top]


def get_confidence_stats(user_id):
    qs = UserQuestionAttempt.objects.filter(user_id=user_id)
    total = qs.count()
    if total == 0:
        return {
            'total_attempted': 0,
            'correct_confident': 0,
            'correct_fragile': 0,
            'wrong_open': 0,
            'fragile_ratio': 0.0,
        }
    correct_confident = qs.filter(last_correct=True, last_confidence=True).count()
    correct_fragile = qs.filter(last_correct=True, last_confidence=False).count()
    wrong_open = qs.filter(ever_correct=False).count()
    correct_total = correct_confident + correct_fragile
    fragile_ratio = (correct_fragile / correct_total * 100) if correct_total else 0.0
    return {
        'total_attempted': total,
        'correct_confident': correct_confident,
        'correct_fragile': correct_fragile,
        'wrong_open': wrong_open,
        'fragile_ratio': round(fragile_ratio, 1),
    }