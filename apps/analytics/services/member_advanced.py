# backend/apps/analytics/services/member_advanced.py
"""
Member-facing advanced analytics — features 1 and 3.

  • Feature 1 — conservative per-category mastery for the caller,
    marking a category as mastered at >= 80 %.
  • Feature 3 — streak history: day-by-day study activity plus the
    caller's current and longest streaks.

Both functions read the caller's own data only; the id is supplied
by the view from `request.user.id`, never from a query parameter.
"""

from datetime import timedelta

from django.utils import timezone

from apps.users.models import User
from apps.learning.mastery import category_mastery_rows
from apps.exams.services.activity import daily_activity


def get_category_mastery(user_id, min_attempts=1, top=12):
    ranked = category_mastery_rows(user_id, min_attempts=min_attempts)

    ranked.sort(key=lambda x: (-x['accuracy'], -x['attempts']))
    return {
        'categories': ranked[:top],
        'total_categories': len(ranked),
    }


def get_streak_history(user_id, days=30):
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30
    days = max(7, min(days, 365))

    now = timezone.localdate()
    cutoff = now - timedelta(days=days - 1)

    by_date = daily_activity(user_id, cutoff)

    day_list = []
    current = cutoff
    one_day = timedelta(days=1)
    while current <= now:
        iso = current.isoformat()
        entry = by_date.get(iso, {'sessions': 0, 'questions': 0})
        day_list.append({
            'date': iso,
            'sessions': entry['sessions'],
            'questions': entry['questions'],
        })
        current += one_day

    user = User.objects.filter(id=user_id).first()

    return {
        'days': day_list,
        'current_streak': (user.current_streak or 0) if user else 0,
        'longest_streak': (user.longest_streak or 0) if user else 0,
        'active_days': sum(1 for d in day_list if d['sessions'] > 0),
        'total_sessions': sum(d['sessions'] for d in day_list),
        'total_questions': sum(d['questions'] for d in day_list),
    }
