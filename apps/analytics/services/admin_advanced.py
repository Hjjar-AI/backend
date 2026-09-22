# backend/apps/analytics/services/admin_advanced.py
"""
Admin-only advanced analytics — features 4 through 8.

  • Feature 4 — difficulty calibration: designed difficulty vs.
    observed accuracy.
  • Feature 5 — author flag rate: which authors' questions get
    flagged most.
  • Feature 6 — exam duration distribution: histogram of session
    durations.
  • Feature 7 — cohort comparison: per-group aggregate comparison.
  • Feature 8 — weekly retention: week-over-week retention.

Every function in this module is gated at the view layer by
'analytics.view_all'. Nothing here performs an authorization check
itself.

NOTE ON `get_author_flag_rate` AND THE FLAGS RELATION
-----------------------------------------------------
The query filters on `Q(flags__isnull=False)`, where `flags` is the
reverse relation from `Question` to `feedback.QuestionFlag`. That
relation is resolved by Django's model layer, not by a Python
import — hence the absence of `from apps.feedback.models import
QuestionFlag` in this module. The original monolithic file carried
that import; it was dead there and is correctly absent here.

NOTE ON `Trunc*` ANNOTATIONS AND MARIADB TIMEZONE TABLES
--------------------------------------------------------
`get_weekly_retention` used to bucket rows by calendar week via
`TruncWeek('completed_at')`. On MariaDB that annotation compiles to

    CONVERT_TZ(completed_at, 'UTC', 'Asia/Damascus')

which requires the `mysql.time_zone*` tables to be populated. On a
stock Debian/Ubuntu MariaDB install those tables are empty, so
`CONVERT_TZ` returns NULL and Django raises

    ValueError: Database returned an invalid datetime value.
    Are time zone definitions for your database installed?

The correct long-term fix is to load the tables once per host:

    sudo mysql_tzinfo_to_sql /usr/share/zoneinfo | sudo mysql mysql

Until that is guaranteed on every deployment, this module does the
week bucketing in Python over a narrow two-column read. The same
decision was already made for `VerificationStatsView` in
`apps/analytics/views.py`, which rewrote its `TruncMonth`
aggregation for exactly this reason. The response shape is
identical either way, so no frontend change is involved.

If a future deployment does guarantee the timezone tables, this
function may be rewritten back to a `TruncWeek` annotation with no
contract change — but the Python path is the portable one today.
"""

from datetime import timedelta

from django.db.models import (
    Avg, Case, Count, IntegerField, Q, Sum, When,
)
from django.utils import timezone

from apps.questions.models import Question
from apps.groups.models import Group, GroupMembership
from apps.exams.models import TestHistory


def get_difficulty_calibration():
    order = ['easy', 'medium', 'hard']
    raw = (
        Question.objects.public()
        .values('difficulty')
        .annotate(
            question_count=Count('id'),
            total_answered=Sum('times_answered'),
            total_correct=Sum('times_correct'),
        )
    )
    by_diff = {r['difficulty']: r for r in raw}

    result = []
    for diff in order:
        r = by_diff.get(diff)
        answered = (r['total_answered'] if r else 0) or 0
        correct = (r['total_correct'] if r else 0) or 0
        observed = round((correct / answered) * 100, 1) if answered else 0.0
        result.append({
            'difficulty': diff,
            'question_count': (r['question_count'] if r else 0) or 0,
            'total_answered': answered,
            'total_correct': correct,
            'observed_accuracy': observed,
        })
    return result


def get_author_flag_rate(min_questions=1, top=20):

    rows = (
        Question.objects.public()
        .filter(authored_by__isnull=False)
        .values('authored_by__username')
        .annotate(
            total_questions=Count('id', distinct=True),
            flagged_questions=Count(
                'id',
                filter=Q(flags__isnull=False),
                distinct=True,
            ),
        )
        .filter(total_questions__gte=min_questions)
    )
    ranked = []
    for r in rows:
        flagged = r['flagged_questions'] or 0
        if flagged == 0:
            continue
        total = r['total_questions'] or 0
        rate = (flagged / total) * 100 if total else 0.0
        ranked.append({
            'author': r['authored_by__username'],
            'total_questions': total,
            'flagged_questions': flagged,
            'flag_rate': round(rate, 1),
        })
    ranked.sort(key=lambda x: (-x['flag_rate'], -x['flagged_questions']))
    return ranked[:top]


def get_exam_duration_distribution(days=90):
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 90
    days = max(1, min(days, 365))

    cutoff = timezone.now() - timedelta(days=days)
    buckets = (
        TestHistory.objects
        .filter(completed_at__gte=cutoff)
        .annotate(
            bucket=Case(
                When(time_spent__lt=300, then=0),
                When(time_spent__lt=900, then=1),
                When(time_spent__lt=1800, then=2),
                When(time_spent__lt=3600, then=3),
                default=4,
                output_field=IntegerField(),
            )
        )
        .values('bucket')
        .annotate(count=Count('id'))
    )
    by_bucket = {r['bucket']: r['count'] for r in buckets}

    labels = [
        ('0-5', '0-5 min'),
        ('5-15', '5-15 min'),
        ('15-30', '15-30 min'),
        ('30-60', '30-60 min'),
        ('60+', '60+ min'),
    ]
    result = []
    for idx, (key, label) in enumerate(labels):
        count = by_bucket.get(idx, 0)
        result.append({
            'bucket': key,
            'label': label,
            'count': count,
        })

    total = sum(r['count'] for r in result)
    for r in result:
        r['percentage'] = round((r['count'] / total) * 100, 1) if total else 0.0

    avg = (
        TestHistory.objects
        .filter(completed_at__gte=cutoff)
        .aggregate(a=Avg('time_spent'))['a']
        or 0
    )
    return {
        'buckets': result,
        'total_sessions': total,
        'avg_seconds': round(avg, 1),
    }


def get_cohort_comparison(days=30):
    """
    Feature 7 — per-group aggregate comparison.

    Query cost: this used to be one members query plus one stats
    aggregate per active group. For a deployment with G active
    groups and S total sessions in the window that was O(G) round
    trips regardless of S. The implementation is now bounded at a
    constant number of queries independent of G:

      1. One query for every group.
      2. One query for every (group, user) membership pair in
         those groups.
      3. One aggregation over TestHistory grouped by user_id,
         filtered to the set of member ids collected in step 2.

    The per-group rollup runs in Python over the aggregated rows.
    The output shape is unchanged: each row carries the same keys
    and the same ordering (by descending accuracy, ties broken by
    descending session count).

    `avg_accuracy` semantics are preserved exactly. The original
    aggregated `AVG(accuracy)` over every session row for every
    member of the group — i.e. each session weighed equally. The
    batched version computes, per user, that user's mean accuracy
    and session count, then rolls up as a session-count-weighted
    mean of the per-user means. Algebraically these are the same
    quantity:
        (sum over users of mean_u * n_u) / (sum over users of n_u)
        == (sum over sessions of accuracy_s) / (count of sessions)
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))

    cutoff = timezone.now() - timedelta(days=days)

    groups = list(Group.objects.filter(is_active=True))
    if not groups:
        return []

    group_by_id = {g.id: g for g in groups}

    # ── Step 1: collect memberships in one query ────────────────────
    #
    # group_members: group_id -> set of user_ids (all members,
    #                regardless of activity — this is what the
    #                original `member_count` field reports).
    # user_to_groups: user_id -> list of group_ids (used to fan a
    #                 user's aggregated session stats back to every
    #                 group they belong to).
    group_members = {}
    user_to_groups = {}
    for row in (
        GroupMembership.objects
        .filter(group__in=groups)
        .values('group_id', 'user_id')
    ):
        gid = row['group_id']
        uid = row['user_id']
        group_members.setdefault(gid, set()).add(uid)
        user_to_groups.setdefault(uid, []).append(gid)

    all_member_ids = list(user_to_groups.keys())
    if not all_member_ids:
        return []

    # ── Step 2: aggregate TestHistory by user ───────────────────────
    user_stats = (
        TestHistory.objects
        .filter(user_id__in=all_member_ids, completed_at__gte=cutoff)
        .values('user_id')
        .annotate(
            session_count=Count('id'),
            avg_accuracy=Avg('accuracy'),
            total_questions=Sum('total_questions'),
        )
    )

    # ── Step 3: roll up per group in Python ─────────────────────────
    #
    # `accuracy_sum` accumulates mean_accuracy * session_count per
    # user so the final division by total session_count yields the
    # same session-weighted mean the original SQL produced.
    group_agg = {}
    for row in user_stats:
        uid = row['user_id']
        sessions = row['session_count'] or 0
        if sessions == 0:
            continue
        acc = row['avg_accuracy'] or 0.0
        tq = row['total_questions'] or 0
        for gid in user_to_groups.get(uid, ()):
            bucket = group_agg.setdefault(gid, {
                'session_count': 0,
                'total_questions': 0,
                'accuracy_sum': 0.0,
                'active_users': set(),
            })
            bucket['session_count'] += sessions
            bucket['total_questions'] += tq
            bucket['accuracy_sum'] += acc * sessions
            bucket['active_users'].add(uid)

    rows = []
    for gid, bucket in group_agg.items():
        group = group_by_id[gid]
        member_count = len(group_members.get(gid, ()))
        avg_accuracy = (
            bucket['accuracy_sum'] / bucket['session_count']
            if bucket['session_count'] else 0.0
        )
        rows.append({
            'group_id': gid,
            'group_name': group.name,
            'member_count': member_count,
            'active_users': len(bucket['active_users']),
            'session_count': bucket['session_count'],
            'total_questions': bucket['total_questions'],
            'avg_accuracy': round(avg_accuracy, 1),
        })

    rows.sort(key=lambda x: (-x['avg_accuracy'], -x['session_count']))
    return rows


def get_weekly_retention(weeks=8):
    """
    Feature 8 — week-over-week retention.

    For each week W in the window, count how many users active in
    the previous week (W-1) were ALSO active in W. Retention rate
    is `retained / active_in_previous_week * 100`.

    "Active in a week" means at least one TestHistory row with
    `completed_at` in that Monday-to-Sunday window.

    Returns the newest week first so the UI can render
    top-down. The oldest week has no prior week to compare
    against and is therefore omitted.

    CONTIGUOUS WEEK SEQUENCE (stability fix)
    ----------------------------------------
    The week list used to be built from
    `sorted(weeks_map.keys())`, which only contains weeks that
    had at least one session. A week with no activity was
    therefore silently dropped from the sequence, and the
    "prior week" of the next active week could be two or more
    calendar weeks earlier. The rate for that row was then
    labelled week-over-week but was actually computed against
    a non-adjacent window — plausible-looking, wrong, and
    impossible to spot from the response alone.

    The sequence is now enumerated from the Monday of the
    cutoff week to the Monday of the current week, so every
    calendar week appears in `sorted_weeks` even when its user
    set is empty. A week whose immediate predecessor is empty
    still skips the row (there is no meaningful retention
    rate), but the skip is now based on the TRUE preceding
    week, not a stale map key.

    PYTHON-SIDE WEEK GROUPING (MariaDB timezone-tables fix)
    -------------------------------------------------------
    This function used to annotate with `TruncWeek('completed_at')`.
    On MariaDB that compiles to a `CONVERT_TZ(...)` call, which
    requires the `mysql.time_zone*` tables to be populated. On a
    stock Debian/Ubuntu MariaDB install those tables are empty, so
    `CONVERT_TZ` returns NULL and Django raises

        ValueError: Database returned an invalid datetime value.
        Are time zone definitions for your database installed?

    The correct long-term fix is to load the tables once per server:

        sudo mysql_tzinfo_to_sql /usr/share/zoneinfo | sudo mysql mysql

    Until that is done on every deployment, this function groups by
    week in Python. This matches the same decision already made for
    `VerificationStatsView` in `apps/analytics/views.py`, which
    rewrote its `TruncMonth` annotation for exactly this reason. The
    response shape is identical either way, so no frontend change is
    involved.

    The query reads only two columns — `user_id` and `completed_at`
    — over the cutoff window. `completed_at` is indexed
    (`th_completed_idx`), so the range scan is cheap, and the
    (user_id, completed_at) pairs are grouped into per-week sets in
    memory. For the volume of rows this app is realistically going
    to hold, the fetch and the Python dict group are negligible.

    WHY NO `distinct()` AND NO `values().distinct()`
    ------------------------------------------------
    The old query ended in `.values('week', 'user_id').distinct()`
    so that a user active multiple times in one week appeared once
    per week. The Python version achieves the same guarantee by
    storing user_ids in a `set` keyed on the week start — duplicate
    (user, week) pairs collapse on insertion, so no SQL-side
    `DISTINCT` is needed and no row is fetched twice.
    """
    try:
        weeks = int(weeks)
    except (TypeError, ValueError):
        weeks = 8
    weeks = max(2, min(weeks, 52))

    # Align to Monday of the current week in local time. The
    # Monday-aligned keys built below match what `TruncWeek` would
    # have produced on a server with the timezone tables loaded.
    now = timezone.now()
    now_local = timezone.localtime(now)
    monday_this_week = (
        now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=now_local.weekday())
    )

    # Fetch one extra week of history so the earliest emitted week
    # has a prior week to compare against.
    cutoff_monday = monday_this_week - timedelta(weeks=weeks + 1)

    rows = (
        TestHistory.objects
        .filter(completed_at__gte=cutoff_monday, user__isnull=False)
        .values_list('user_id', 'completed_at')
    )

    # Group (user_id, completed_at) pairs into per-week sets of
    # active users. A `set` collapses duplicates, so a user who
    # completed multiple sessions in one week appears once for that
    # week — the same guarantee the previous `.distinct()` provided.
    weeks_map = {}
    for user_id, completed_at in rows:
        if completed_at is None:
            continue
        local_dt = timezone.localtime(completed_at)
        week_start = (
            local_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=local_dt.weekday())
        )
        weeks_map.setdefault(week_start, set()).add(user_id)

    # Contiguous list of week-start datetimes from the cutoff Monday
    # through the Monday of the current week. This is the fix
    # described in the docstring: the sequence includes weeks that
    # had no activity, so adjacency is preserved.
    sorted_weeks = []
    cursor = cutoff_monday
    while cursor <= now_local:
        sorted_weeks.append(cursor)
        cursor += timedelta(weeks=1)

    if len(sorted_weeks) < 2:
        return []

    result = []
    for i in range(1, len(sorted_weeks)):
        prior = weeks_map.get(sorted_weeks[i - 1], set())
        current = weeks_map.get(sorted_weeks[i], set())
        if not prior:
            # A week whose immediate predecessor had no active
            # users has no meaningful retention rate. Skip the
            # row — the previous behaviour for this specific
            # case is preserved.
            continue
        retained = len(prior & current)
        rate = (retained / len(prior)) * 100
        result.append({
            'week_start': sorted_weeks[i].isoformat(),
            'active_prior_week': len(prior),
            'active_this_week': len(current),
            'retained': retained,
            'retention_rate': round(rate, 1),
        })

    result.sort(key=lambda x: x['week_start'], reverse=True)
    return result[:weeks]