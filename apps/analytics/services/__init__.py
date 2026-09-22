# backend/apps/analytics/services/__init__.py
"""
Package surface for the analytics services.

Split from a single module into three sibling modules, grouped by
audience — the same grouping the section banners in the monolithic
file already used:

  • member.py          — the original member-facing methods
                         (category coverage, tag coverage,
                         difficulty stats, per-user performance
                         trend, active-users stats, weak categories,
                         confidence stats).
  • member_advanced.py — the two member-facing advanced reports
                         (features 1 and 3): category mastery and
                         streak history.
  • admin_advanced.py  — the five admin-only reports (features 4
                         through 8): difficulty calibration, author
                         flag rate, exam duration histogram, cohort
                         comparison, weekly retention.

`AnalyticsService` is a thin façade class that re-exports every
public function as a static method, so existing call sites
(`AnalyticsService.get_user_performance_trend(...)`,
`from .services import AnalyticsService`) keep working unchanged.

Call sites that reference the façade:
  • apps/analytics/views.py         — `from .services import AnalyticsService`
  • apps/learning/services.py       — `from apps.analytics.services import AnalyticsService`

Both go through this `__init__.py`; neither needs to be edited.
"""

from . import admin_advanced, member, member_advanced


class AnalyticsService:

    # ══════════════════════════════════════════════════════════════════
    # Existing member-facing (unchanged)
    # ══════════════════════════════════════════════════════════════════
    get_category_coverage = staticmethod(member.get_category_coverage)
    get_tag_coverage = staticmethod(member.get_tag_coverage)
    get_difficulty_stats = staticmethod(member.get_difficulty_stats)
    get_user_performance_trend = staticmethod(member.get_user_performance_trend)
    get_active_users_stats = staticmethod(member.get_active_users_stats)
    get_user_weak_categories = staticmethod(member.get_user_weak_categories)
    get_confidence_stats = staticmethod(member.get_confidence_stats)

    # ══════════════════════════════════════════════════════════════════
    # MEMBER-FACING ADVANCED — features 1 and 3
    # ══════════════════════════════════════════════════════════════════
    get_category_mastery = staticmethod(member_advanced.get_category_mastery)
    get_streak_history = staticmethod(member_advanced.get_streak_history)

    # ══════════════════════════════════════════════════════════════════
    # ADMIN ADVANCED — features 4 through 8
    # ══════════════════════════════════════════════════════════════════
    get_difficulty_calibration = staticmethod(
        admin_advanced.get_difficulty_calibration
    )
    get_author_flag_rate = staticmethod(admin_advanced.get_author_flag_rate)
    get_exam_duration_distribution = staticmethod(
        admin_advanced.get_exam_duration_distribution
    )
    get_cohort_comparison = staticmethod(admin_advanced.get_cohort_comparison)
    get_weekly_retention = staticmethod(admin_advanced.get_weekly_retention)


__all__ = ['AnalyticsService']