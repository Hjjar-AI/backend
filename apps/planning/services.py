# backend/apps/planning/services.py

from datetime import datetime

from django.db.models import Sum
from django.utils import timezone

from apps.exams.models import TestHistory
from apps.exams.services.activity import daily_activity
from apps.questions.models import Tag, clean_tag_name

from .models import StudyPlanner, StudyPlannerDay


class StudyPlannerService:

    @staticmethod
    def get_or_create_planner(user):
        planner, created = StudyPlanner.objects.get_or_create(user=user)
        return planner

    @staticmethod
    def update_planner(user, target, category_ids, tag_names, start_date, end_date):
        """
        Replace the planner's configuration.

        `category_ids` is a list of category ids. Ids that do not
        resolve to a live Category are silently dropped.
        `tag_names` is a list of tag names. Names that do not resolve
        to an existing Tag are created on the fly — this matches the
        previous behaviour where target_tags was a free-form list of
        names.

        Both lists are treated as full replacements: whatever was
        there before is cleared and replaced with the incoming set.
        """
        planner, _ = StudyPlanner.objects.get_or_create(user=user)
        planner.target_questions_per_day = target
        planner.start_date = start_date
        planner.end_date = end_date
        planner.save(update_fields=[
            'target_questions_per_day', 'start_date', 'end_date', 'updated_at',
        ])

        # ── Categories ────────────────────────────────────────────
        if category_ids:
            from apps.questions.models import Category
            cats = Category.objects.filter(id__in=category_ids)
            planner.target_categories.set(cats)
        else:
            planner.target_categories.clear()

        # ── Tags ──────────────────────────────────────────────────
        if tag_names:
            tag_objects = []
            seen = set()
            for raw in tag_names:
                name = clean_tag_name(str(raw))
                if not name or name in seen:
                    continue
                seen.add(name)
                tag, _ = Tag.objects.get_or_create(name=name)
                tag_objects.append(tag)
            planner.target_tags.set(tag_objects)
        else:
            planner.target_tags.clear()

        return planner

    @staticmethod
    def record_daily_progress(user):
        """
        Upsert today's questions-answered count.

        ACTIVITY SOURCE (fix — issue: planner disagrees with the rest
        of the activity system)
        ---------------------------------------------------------
        This method used to aggregate `TestHistory` only. The shared
        activity calculation
        (`apps.exams.services.activity.daily_activity`, used by the
        heatmap, the streak history, and the group leaderboard) counts
        BOTH `TestHistory` completions and `MasterExamAttempt`
        completions — every completed master exam contributes its
        `total_questions` to the day's count.

        The mismatch meant a user who completed a master exam saw the
        completion move their streak and heatmap but not their
        planner's "questions answered today," and never reached their
        daily target if the exam was the only activity of the day.

        The planner now reads the same `daily_activity` helper the
        rest of the system uses. The day boundary is `timezone.localdate()`
        on both sides (the helper truncates on the same local timezone
        the app is configured with), so the two views of "today"
        cannot drift.

        HOT PATH (unchanged)
        --------------------
        The fast path for a user whose planner row already exists is
        still two queries: one activity aggregate and one UPDATE. When
        the UPDATE affects zero rows (first study day for the account,
        or the day row was manually removed), the fallback path sets
        up the planner and inserts the day row.
        """
        today = timezone.localdate()

        # The activity helper takes a first_date and returns a dict
        # keyed on ISO date. Fetching with `first_date=today` bounds
        # the query to the current local day on both source tables.
        activity = daily_activity(user.id, today)
        answered_today = activity.get(today.isoformat(), {}).get('questions', 0) or 0

        # Fast path: the (planner, date) row exists. A single UPDATE
        # against the indexed pair.
        updated = (
            StudyPlannerDay.objects
            .filter(planner__user=user, date=today)
            .update(questions_answered=answered_today)
        )

        # Slow path: first study day for this account, or the day
        # row was manually removed. Set up the planner, then insert
        # the day.
        if not updated:
            planner, _ = StudyPlanner.objects.get_or_create(user=user)
            StudyPlannerDay.objects.update_or_create(
                planner=planner,
                date=today,
                defaults={'questions_answered': answered_today},
            )

        return answered_today

    @staticmethod
    def delete_planner(user):
        # Cascades to StudyPlannerDay via the FK.
        StudyPlanner.objects.filter(user=user).delete()