# backend/apps/analytics/views.py

from collections import Counter

from django.db.models import Count, Sum, Case, When, IntegerField, Q
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from .services import AnalyticsService
from apps.core.permissions import HasCapability
from apps.core.utils import api_success, api_error, safe_int
from apps.questions.models import Question, Category


class SummaryView(APIView):
    """
    Personal analytics for every authenticated user.

    Admin-level sections (category coverage, tag coverage, difficulty
    stats, active users) are only included when the caller holds
    'analytics.view_all'. The response shape is stable either way —
    the admin-only fields come back as empty lists for users who
    cannot see them, so the frontend does not have to conditionally
    destructure.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        days = safe_int(
            request.query_params.get('days'), 30,
            minimum=1, maximum=90,
        )
        can_view_all = request.user.has_capability('analytics.view_all')

        data = {
            'user_performance': AnalyticsService.get_user_performance_trend(
                request.user.id, days,
            ),
            'weak_categories': AnalyticsService.get_user_weak_categories(
                request.user.id,
            ),
            'confidence_stats': AnalyticsService.get_confidence_stats(
                request.user.id,
            ),
        }

        if can_view_all:
            data['category_coverage'] = AnalyticsService.get_category_coverage()
            data['tag_coverage'] = AnalyticsService.get_tag_coverage()
            data['difficulty_stats'] = AnalyticsService.get_difficulty_stats()
            data['active_users'] = AnalyticsService.get_active_users_stats(days)
        else:
            data['category_coverage'] = []
            data['tag_coverage'] = []
            data['difficulty_stats'] = []
            data['active_users'] = []

        return api_success(data=data)


class ActiveUsersStatsView(APIView):
    """
    Day-by-day active/new user counts. Requires
    'admin.active_users'.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.active_users'

    def get(self, request):
        days = safe_int(
            request.query_params.get('days'), 30,
            minimum=1, maximum=90,
        )
        data = AnalyticsService.get_active_users_stats(days)
        return api_success(data=data)


class VerificationStatsView(APIView):
    """
    Global verification coverage. Requires
    'admin.verification_stats'.

    MONTHLY AGGREGATION — PYTHON-SIDE GROUPING
    ------------------------------------------
    The monthly bucket used to be computed with
    `TruncMonth('verified_at')`. On MariaDB that compiles to

        CONVERT_TZ(verified_at, 'UTC', 'Asia/Damascus')

    which requires the `mysql.time_zone*` tables to be populated.
    On a stock Debian/Ubuntu MariaDB install those tables are empty,
    so `CONVERT_TZ` returns NULL and Django raises:

        ValueError: Database returned an invalid datetime value.
        Are time zone definitions for your database installed?

    The correct long-term fix is to load the tables once per server:

        sudo mysql_tzinfo_to_sql /usr/share/zoneinfo | sudo mysql mysql

    Doing so restores every `Trunc*` call on a tz-aware DateTimeField
    across the whole app — this endpoint, `get_weekly_retention`, and
    anything future that adds a `Trunc` annotation.

    Until that is done on every deployment, this endpoint groups by
    month in Python. The query is a single indexed column read on
    `(verified, verified_at)`; for the number of verified questions
    this app is realistically going to hold it is negligible. If the
    endpoint ever becomes hot, load the tz tables and restore the
    original `TruncMonth` aggregation — the response shape is
    identical either way, so no frontend change is involved.

    The `TruncMonth` import that used to live at the top of this file
    has been removed because it is no longer referenced anywhere in
    the module. When the aggregation is restored, re-add it.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.verification_stats'

    def get(self, request):
        public = Question.objects.public()

        total_verified = public.filter(verified=True).count()
        total_unverified = public.filter(verified=False).count()
        total = total_verified + total_unverified
        rate = (total_verified / total * 100) if total > 0 else 0

        by_user = (
            public
            .filter(verified=True, verified_by__isnull=False)
            .values('verified_by')
            .annotate(count=Count('id'))
            .order_by('-count')
        )

        by_category = (
            Category.objects.annotate(
                total=Count(
                    'questions',
                    filter=Q(questions__is_draft=False),
                ),
                verified=Count(
                    'questions',
                    filter=Q(
                        questions__verified=True,
                        questions__is_draft=False,
                    ),
                ),
            )
            .values('name', 'color', 'total', 'verified')
        )

        # ── Monthly verified counts, last 12 months ────────────────
        #
        # Python-side grouping. See the class docstring for why this
        # replaced a `TruncMonth` annotation and when to restore it.
        #
        # `verified_at` is stored tz-aware (UTC). `timezone.localtime`
        # converts to TIME_ZONE (Asia/Damascus) before extracting the
        # year and month, so a question verified at 22:00 UTC on the
        # last day of the month lands in the NEXT month locally —
        # which is the same behaviour the SQL version produced when
        # the tz tables were loaded.
        verified_dates = public.filter(
            verified=True,
            verified_at__isnull=False,
        ).values_list('verified_at', flat=True)

        month_counts = Counter()
        for dt in verified_dates:
            local = timezone.localtime(dt)
            month_counts[(local.year, local.month)] += 1

        top_months = sorted(
            month_counts.items(),
            key=lambda kv: (kv[0][0], kv[0][1]),
            reverse=True,
        )[:12]

        monthly = [
            {'month': f'{year:04d}-{month:02d}', 'count': count}
            for (year, month), count in top_months
        ]

        return api_success(data={
            'total_verified': total_verified,
            'total_unverified': total_unverified,
            'verification_rate': rate,
            'by_user': list(by_user),
            'by_category': list(by_category),
            'monthly': monthly,
        })


# ═══════════════════════════════════════════════════════════════════════
# MEMBER-FACING ADVANCED — features 1 and 3
# ═══════════════════════════════════════════════════════════════════════

class CategoryMasteryView(APIView):
    """
    Feature 1 — the caller's own category mastery.

    Reads the user id from the session, never from a query
    parameter. A member cannot request another user's mastery even
    by adding ?user_id= to the URL; the parameter is simply ignored.

    Optional query params:
        min_attempts  — default 1, minimum 1, maximum 50
        top           — default 12, minimum 1, maximum 50
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        min_attempts = safe_int(
            request.query_params.get('min_attempts'), 1,
            minimum=1, maximum=50,
        )
        top = safe_int(
            request.query_params.get('top'), 12,
            minimum=1, maximum=50,
        )

        data = AnalyticsService.get_category_mastery(
            request.user.id,
            min_attempts=min_attempts,
            top=top,
        )
        return api_success(data=data)


class StreakHistoryView(APIView):
    """
    Feature 3 — the caller's own day-by-day study activity.

    Same ownership rule as CategoryMasteryView: the user id comes
    from the session.

    Optional query params:
        days  — default 30, minimum 7, maximum 365
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        days = safe_int(
            request.query_params.get('days'), 30,
            minimum=7, maximum=365,
        )
        data = AnalyticsService.get_streak_history(request.user.id, days=days)
        return api_success(data=data)


# ═══════════════════════════════════════════════════════════════════════
# ADMIN ADVANCED — features 4 through 8
# ═══════════════════════════════════════════════════════════════════════
#
# Every view in this section requires 'analytics.view_all'. They are
# deliberately separate endpoints rather than one endpoint with a
# `report=` query param:
#
#   • Each report has a different query shape and a different
#     optional-parameter set. A combined endpoint would need to
#     branch on the query param anyway, and any per-report
#     throttling or cache tuning would have to be special-cased
#     inside the shared view.
#
#   • The frontend loads each report lazily when the user expands
#     its accordion section. Separate URLs mean each request fetches
#     exactly what that section needs, and a slow report does not
#     block the others.
#
#   • Independent routes are easier to grep for in server logs when
#     one of them is slow.

class DifficultyCalibrationView(APIView):
    """
    Feature 4 — designed difficulty versus observed accuracy.

    No query parameters. The report is a snapshot of the bank, not
    a time-windowed view, because the whole point is to reveal a
    mismatch between what the author labelled and what the takers
    actually did — which the current question counts encode.
    """
    permission_classes = [HasCapability]
    required_capability = 'analytics.view_all'

    def get(self, request):
        data = AnalyticsService.get_difficulty_calibration()
        return api_success(data={'rows': data})


class AuthorFlagRateView(APIView):
    """
    Feature 5 — flagged-question rate per author.

    Optional query params:
        min_questions  — default 1, minimum 1, maximum 500.
                         Filters out authors whose question count is
                         too small to make a rate meaningful.
        top            — default 20, minimum 1, maximum 100
    """
    permission_classes = [HasCapability]
    required_capability = 'analytics.view_all'

    def get(self, request):
        min_questions = safe_int(
            request.query_params.get('min_questions'), 1,
            minimum=1, maximum=500,
        )
        top = safe_int(
            request.query_params.get('top'), 20,
            minimum=1, maximum=100,
        )

        data = AnalyticsService.get_author_flag_rate(
            min_questions=min_questions,
            top=top,
        )
        return api_success(data={'rows': data})


class ExamDurationView(APIView):
    """
    Feature 6 — session duration histogram.

    Optional query params:
        days  — default 90, minimum 1, maximum 365
    """
    permission_classes = [HasCapability]
    required_capability = 'analytics.view_all'

    def get(self, request):
        days = safe_int(
            request.query_params.get('days'), 90,
            minimum=1, maximum=365,
        )
        data = AnalyticsService.get_exam_duration_distribution(days=days)
        return api_success(data=data)


class CohortComparisonView(APIView):
    """
    Feature 7 — per-group aggregate comparison.

    Optional query params:
        days  — default 30, minimum 1, maximum 365
    """
    permission_classes = [HasCapability]
    required_capability = 'analytics.view_all'

    def get(self, request):
        days = safe_int(
            request.query_params.get('days'), 30,
            minimum=1, maximum=365,
        )
        rows = AnalyticsService.get_cohort_comparison(days=days)
        return api_success(data={'rows': rows})


class WeeklyRetentionView(APIView):
    """
    Feature 8 — week-over-week retention.

    Optional query params:
        weeks  — default 8, minimum 2, maximum 52
    """
    permission_classes = [HasCapability]
    required_capability = 'analytics.view_all'

    def get(self, request):
        weeks = safe_int(
            request.query_params.get('weeks'), 8,
            minimum=2, maximum=52,
        )
        rows = AnalyticsService.get_weekly_retention(weeks=weeks)
        return api_success(data={'rows': rows})