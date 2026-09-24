# backend/apps/exams/services/activity.py
"""Completed study activity across regular sessions and master exams.

MARIADB TIMEZONE-TABLES CAVEAT
------------------------------
`daily_activity` used to depend on MariaDB's timezone tables in TWO
places:

  1. The filter `{timestamp}__date__gte=first_date` — Django's
     `__date` lookup on a tz-aware DateTimeField compiles to a
     `CONVERT_TZ` on MariaDB. With the tables empty, `CONVERT_TZ`
     returns NULL, the comparison evaluates to NULL, and every row
     is filtered out. The query returns nothing — silently.

  2. The annotation `TruncDate(timestamp)` — same failure mode, but
     this one raises `ValueError: Database returned an invalid
     datetime value` because Django's result converter rejects a
     NULL where it expected a date.

`group_activity` does not use either construct; it groups by
`user_id` (an integer) and sums integer columns, so it is unaffected
by the timezone-table state.

`daily_activity` has been rewritten to do both the range filter and
the day bucketing in Python, so no `CONVERT_TZ` reaches the SQL. See
the function docstring for the details.
"""

from datetime import datetime, time

from django.db.models import Count, Sum
from django.utils import timezone

from apps.exams.models import TestHistory
from apps.master_exams.models import MasterExamAttempt


def daily_activity(user_id, first_date):
    """
    Return a `{iso_date: {sessions, questions}}` map for one user,
    covering all activity on or after `first_date` (a local date).

    Both regular sessions (`TestHistory.completed_at`) and completed
    master exams (`MasterExamAttempt.finished_at`) contribute. A day
    with activity from either source appears in the map; a day with
    activity from both sums the two series.

    PYTHON-SIDE DAY GROUPING (MariaDB timezone-tables fix)
    -------------------------------------------------------
    The filter and the annotation are both rewritten. See the module
    docstring for the failure modes this avoids.

    The filter is expressed as a direct `timestamp__gte` comparison
    against an aware datetime representing midnight local on
    `first_date`. That is exactly the instant the previous
    `{timestamp}__date__gte=first_date` lookup compared against —
    the `__date` lookup's job is precisely to compute "midnight
    local on this date, in the field's storage timezone" — but it
    does the computation without any SQL-side timezone conversion.

    `local_start` is passed into the filter unchanged, not converted
    to UTC first. Django's database layer normalises any aware
    datetime to UTC for the query when `USE_TZ=True`, so the two
    forms are the same instant. Converting here would work too but
    would require importing `datetime.timezone`, and the extra step
    would obscure the intent. `django.utils.timezone.utc` — which an
    earlier draft of this rewrite used — was deprecated in Django
    4.0 and removed in Django 5.0, so it is not available on the
    Django versions this app targets.

    The bucketing is done with
    `timezone.localtime(timestamp).date()` in the loop, matching
    what `TruncDate` would have produced.

    SIGNATURE AND CALLER CONTRACT
    -----------------------------
    Unchanged: `(user_id, first_date)`, with `first_date` a
    `datetime.date`. Every existing caller —
    `member_advanced.get_streak_history`,
    `StudyPlannerService.record_daily_progress`, and
    `ActivityHeatmapView.get` — passes a date obtained from
    `timezone.localdate()` or from `.date()` on a localised
    datetime, so the caller contract is preserved.

    DST NOTE
    --------
    `first_date` is interpreted as midnight in the current timezone
    (`settings.TIME_ZONE`, i.e. `Asia/Damascus`). On a DST
    spring-forward day, that wall-clock time does not exist; the
    `datetime.replace(tzinfo=...)` path Python 3.9+ uses for
    `zoneinfo` resolves it to the pre-transition offset, which lands
    on the same UTC instant the previous `__date` lookup compared
    against. On a fall-back day the first of the two occurrences of
    midnight is used — again the same instant the SQL-side path
    produced. Behaviour at the boundary is therefore unchanged. In
    practice Damascus's DST transitions happen at 00:00 on a
    Sunday, and callers pass either "today" or "N days ago", so the
    boundary case is vanishingly rare.
    """
    local_tz = timezone.get_current_timezone()
    # Aware datetime at local midnight on `first_date`. Passed into
    # the query filter below; Django normalises it to UTC for the
    # comparison against the tz-aware column.
    local_start = timezone.make_aware(
        datetime.combine(first_date, time.min),
        local_tz,
    )

    activity = {}
    for model, timestamp in (
        (TestHistory, 'completed_at'),
        (MasterExamAttempt, 'finished_at'),
    ):
        rows = (
            model.objects
            .filter(user_id=user_id, **{f'{timestamp}__gte': local_start})
            .values_list(timestamp, 'answered_count')
        )
        for ts, questions in rows:
            # `TestHistory.completed_at` is non-nullable;
            # `MasterExamAttempt.finished_at` is nullable (an
            # in-progress attempt has no finish timestamp). The
            # guard is required for the latter and defensive for
            # the former.
            if ts is None:
                continue
            day_key = timezone.localtime(ts).date().isoformat()
            bucket = activity.setdefault(
                day_key, {'sessions': 0, 'questions': 0},
            )
            bucket['sessions'] += 1
            bucket['questions'] += questions or 0
    return activity


def group_activity(user_ids, cutoff):
    activity = {}
    for model, timestamp in (
        (TestHistory, 'completed_at'),
        (MasterExamAttempt, 'finished_at'),
    ):
        rows = (
            model.objects
            .filter(user_id__in=user_ids, **{f'{timestamp}__gte': cutoff})
            .values('user_id')
            .annotate(
                questions_answered=Sum('answered_count'),
                correct=Sum('correct_count'),
                sessions=Count('id'),
            )
        )
        for row in rows:
            user = activity.setdefault(row['user_id'], {
                'questions_answered': 0, 'correct': 0, 'sessions': 0,
            })
            for field in user:
                user[field] += row[field] or 0
    return activity
