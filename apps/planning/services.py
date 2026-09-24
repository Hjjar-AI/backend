# backend/apps/planning/services.py

from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.exams.models import TestHistory
from apps.master_exams.models import MasterExamAttempt
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
        """Upsert today's answered count within the active plan scope.

        Both ordinary and master exams contribute. When category/tag
        targets exist, only answered result rows matching either target count.
        """
        today = timezone.localdate()

        planner, _ = StudyPlanner.objects.prefetch_related(
            'target_categories', 'target_tags',
        ).get_or_create(user=user)

        # A dated plan does not accrue progress before it starts or after it
        # ends. This prevents old/general activity from satisfying a new plan.
        in_window = (
            today >= planner.start_date
            and (planner.end_date is None or today <= planner.end_date)
        )
        answered_today = 0
        if in_window:
            local_tz = timezone.get_current_timezone()
            day_start = timezone.make_aware(datetime.combine(today, time.min), local_tz)
            day_end = day_start + timedelta(days=1)
            category_ids = set(
                planner.target_categories.values_list('id', flat=True)
            )
            tag_names = set(
                planner.target_tags.values_list('name', flat=True)
            )
            is_targeted = bool(category_ids or tag_names)

            regular_rows = TestHistory.objects.filter(
                user=user, completed_at__gte=day_start, completed_at__lt=day_end,
            ).values_list('results', 'answered_count')
            master_rows = MasterExamAttempt.objects.filter(
                user=user, finished_at__gte=day_start, finished_at__lt=day_end,
                is_complete=True,
            ).values_list('results', 'answered_count')

            for stored_results, answered_count in list(regular_rows) + list(master_rows):
                if not is_targeted:
                    answered_today += answered_count or 0
                    continue
                questions = (
                    stored_results.get('questions', [])
                    if isinstance(stored_results, dict)
                    else stored_results or []
                )
                for result in questions:
                    if result.get('user_answer') is None:
                        continue
                    result_tags = set(result.get('tag_names') or [])
                    if (
                        result.get('category_id') in category_ids
                        or bool(result_tags & tag_names)
                    ):
                        answered_today += 1

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
