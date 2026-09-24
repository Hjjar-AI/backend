
# backend/apps/learning/services.py

from django.db.models import Q
from django.utils import timezone

from .srs_service import SRSService
from apps.questions.models import Question


class LearningService:

    @staticmethod
    def study_now_queue(user, limit=20):
       
        if user is None or not getattr(user, 'is_authenticated', False):
            return {'question_ids': [], 'total': 0, 'breakdown': {
                'srs_due': 0, 'fragile': 0, 'wrong_open': 0,
                'planner_targets': 0, 'weak_categories': 0,
                'fresh': 0, 'general': 0,
            }}

        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 200))

        budgets = {
            'srs_due': max(1, int(limit * 0.35)),
            'planner_targets': int(limit * 0.25),
            'fragile': int(limit * 0.15),
            'wrong_open': int(limit * 0.15),
        }
        budgets['weak_categories'] = limit - sum(budgets.values())

        seen = set()
        result = []
        actual = {
            'srs_due': 0, 'planner_targets': 0, 'fragile': 0,
            'wrong_open': 0, 'weak_categories': 0, 'fresh': 0,
            'general': 0,
        }

        def _pull(source_ids, budget, key):
            """Pull up to `budget` new ids from source_ids into result."""
            taken = 0
            for qid in source_ids:
                if taken >= budget:
                    break
                if qid in seen:
                    continue
                seen.add(qid)
                result.append(qid)
                taken += 1
            actual[key] = taken
            return taken

        # 1. SRS-due. The scheduler already ranks these by urgency.
        srs_due_ids = SRSService.due_question_ids(user)
        # 2. Active planner targets are a real source of the queue, not just
        # display metadata. Category and tag targets are ORed, matching the
        # planner progress rule.
        planner_ids = []
        try:
            from apps.planning.models import StudyPlanner
            planner = StudyPlanner.objects.prefetch_related(
                'target_categories', 'target_tags',
            ).get(user=user)
            today = timezone.localdate()
            active = (
                today >= planner.start_date
                and (planner.end_date is None or today <= planner.end_date)
            )
            category_ids = list(planner.target_categories.values_list('id', flat=True))
            tag_ids = list(planner.target_tags.values_list('id', flat=True))
            if active and (category_ids or tag_ids):
                target_filter = Q()
                if category_ids:
                    target_filter |= Q(category_id__in=category_ids)
                if tag_ids:
                    target_filter |= Q(tags__id__in=tag_ids)
                planner_ids = list(
                    Question.objects.public()
                    .filter(target_filter)
                    .distinct()
                    .order_by('?')
                    .values_list('id', flat=True)[:limit * 3]
                )
        except StudyPlanner.DoesNotExist:
            planner_ids = []

        # 3. Fragile correct. Ordered by recency of the fragile answer.
        fragile_ids = SRSService.fragile_question_ids(user)
        # 4. Current lapses, including questions that were once correct.
        wrong_ids = SRSService.wrong_question_ids(user)

        pulled = _pull(srs_due_ids, budgets['srs_due'], 'srs_due')
        deficit = budgets['srs_due'] - pulled
        if deficit > 0:
            budgets['planner_targets'] += deficit

        pulled = _pull(planner_ids, budgets['planner_targets'], 'planner_targets')
        deficit = budgets['planner_targets'] - pulled
        if deficit > 0:
            budgets['fragile'] += deficit

        pulled = _pull(fragile_ids, budgets['fragile'], 'fragile')
        deficit = budgets['fragile'] - pulled
        if deficit > 0:
            budgets['wrong_open'] += deficit

        pulled = _pull(wrong_ids, budgets['wrong_open'], 'wrong_open')
        deficit = budgets['wrong_open'] - pulled
        if deficit > 0:
            budgets['weak_categories'] += deficit

        # 5. Weak categories. Uses the shared conservative mastery ranking.
        # (lowest accuracy, ties broken by attempt volume). Pulls
        # questions in those categories that the user has NOT already
        # seen in this session.
        weak_ids = []
        try:
            from apps.analytics.services import AnalyticsService
            weak = AnalyticsService.get_user_weak_categories(user.id, min_attempts=3, top=3)
            weak_cat_ids = [w['category_id'] for w in weak if w.get('category_id')]
            if weak_cat_ids:
                weak_qs = (
                    Question.objects.public()
                    .filter(category_id__in=weak_cat_ids)
                    .exclude(id__in=seen)
                    .order_by('?')
                    .values_list('id', flat=True)[:budgets['weak_categories'] * 3]
                )
                weak_ids = list(weak_qs)
        except Exception:
            # Analytics is a hint, not a hard dependency. If it fails
            # (empty population, missing import), skip this bucket and
            # let the fallback fill the remainder.
            weak_ids = []

        _pull(weak_ids, budgets['weak_categories'], 'weak_categories')

        # 6. Fresh fallback. Fill any remaining slots with public
        # questions the user has never attempted. This keeps a brand-new
        # account from getting an empty session.
        if len(result) < limit:
            remaining = limit - len(result)
            fresh_qs = (
                Question.objects.public()
                .exclude(id__in=seen)
                .exclude(user_attempts__user=user)
                .order_by('?')
                .values_list('id', flat=True)[:remaining]
            )
            for qid in fresh_qs:
                if qid in seen:
                    continue
                seen.add(qid)
                result.append(qid)
                actual['fresh'] += 1

        # If the unseen pool is smaller than the requested queue, fill from
        # any remaining public questions. This guarantees the requested size
        # whenever the bank itself contains enough questions.
        if len(result) < limit:
            remaining = limit - len(result)
            general_qs = (
                Question.objects.public()
                .exclude(id__in=seen)
                .order_by('?')
                .values_list('id', flat=True)[:remaining]
            )
            for qid in general_qs:
                seen.add(qid)
                result.append(qid)
                actual['general'] += 1

        return {
            'question_ids': result,
            'total': len(result),
            'breakdown': actual,
        }
