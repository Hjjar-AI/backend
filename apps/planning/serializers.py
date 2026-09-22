# backend/apps/planning/serializers.py

from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .models import StudyPlanner


class StudyPlannerSerializer(serializers.ModelSerializer):
    """
    Read shape (unchanged from the previous JSONField version):

        {
          "id": 1,
          "target_questions_per_day": 10,
          "target_categories": [1, 4, 7],          // category ids
          "target_tags": ["فصام", "CBT"],           // tag names
          "start_date": "2026-01-01",
          "end_date": null,
          "daily_progress": {"2026-01-01": 5, ...},
          "today": "2026-01-01"
        }

    `daily_progress` is bounded to the last 90 days so the payload
    does not grow without bound for a long-lived account. The frontend
    only consumes the last 7 days for the weekly view plus today for
    the target-reached check; 90 days is a comfortable margin.
    """
    today = serializers.SerializerMethodField()
    target_categories = serializers.SerializerMethodField()
    target_tags = serializers.SerializerMethodField()
    daily_progress = serializers.SerializerMethodField()

    class Meta:
        model = StudyPlanner
        fields = [
            'id', 'target_questions_per_day', 'target_categories', 'target_tags',
            'start_date', 'end_date', 'daily_progress', 'today',
        ]
        read_only_fields = [
            'id', 'target_categories', 'target_tags', 'daily_progress',
            'created_at', 'updated_at',
        ]

    def get_today(self, obj):
        return timezone.localdate().isoformat()

    def get_target_categories(self, obj):
        # Prefetchable if the view is ever changed to a list response;
        # for a single-planner read this is one indexed query.
        return list(obj.target_categories.values_list('id', flat=True))

    def get_target_tags(self, obj):
        return list(obj.target_tags.values_list('name', flat=True))

    def get_daily_progress(self, obj):
        # When the view supplied a Prefetch with `to_attr`, use the
        # already-hydrated list and skip the query entirely. When it
        # did not (create/update responses, tests, ad-hoc use), fall
        # back to a single filtered query. The attribute name matches
        # the `to_attr` set on the Prefetch in GetPlannerView.
        prefetched = getattr(obj, 'recent_days_prefetched', None)
        if prefetched is not None:
            return {
                d.date.isoformat(): d.questions_answered
                for d in prefetched
            }

        cutoff = timezone.localdate() - timedelta(days=90)
        rows = (
            obj.days
            .filter(date__gte=cutoff)
            .values_list('date', 'questions_answered')
        )
        return {d.isoformat(): c for d, c in rows}