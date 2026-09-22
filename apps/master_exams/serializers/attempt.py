# backend/apps/master_exams/serializers/attempt.py
"""
Serializers for the attempt lifecycle: start, status, submit answer,
goto, and flag.

The `master_exam` attribute is always cached on the instance that
reaches `MasterExamAttemptSerializer` — see the docstring on
`get_duration_minutes` for the full contract.
"""

from django.conf import settings
from rest_framework import serializers

from ..models import MasterExamAttempt
from ._constants import MAX_CHOICES


class MasterExamAttemptStartSerializer(serializers.Serializer):
    preview = serializers.BooleanField(required=False, default=False)


class MasterExamAttemptSerializer(serializers.ModelSerializer):
    master_exam_id = serializers.IntegerField(read_only=True)
    exam_name = serializers.CharField(source='exam_name_snapshot', read_only=True)
    is_active = serializers.SerializerMethodField()
    duration_minutes = serializers.SerializerMethodField()
    grace_seconds = serializers.SerializerMethodField()

    class Meta:
        model = MasterExamAttempt
        fields = [
            'id', 'master_exam_id', 'session_id',
            'exam_name',
            'is_active', 'is_complete', 'is_makeup', 'forced_finish',
            'question_ids', 'answers', 'current_question_id',
            'started_at', 'deadline_at', 'finished_at',
            'duration_minutes', 'grace_seconds',
            'correct_count', 'total_questions', 'accuracy', 'weighted_score',
        ]
        read_only_fields = fields

    def get_is_active(self, obj):
        return not obj.is_complete

    def get_duration_minutes(self, obj):
        # `obj.master_exam` is always a cached attribute on every path
        # that instantiates this serializer:
        #
        #   • Create path (MasterExamStartAttemptView → service.start):
        #     `MasterExamAttempt.objects.create(master_exam=exam, ...)`
        #     caches the passed `exam` on the returned instance, so the
        #     access below is a Python attribute read.
        #   • Finish path: `_finish_locked` reads the row through
        #     `.select_related('master_exam')`.
        #   • List / detail path: `get_my_attempt` in
        #     `MasterExamListSerializer` reads from a
        #     `Prefetch(..., queryset=...select_related('master_exam'))`
        #     whose queryset carries the select_related.
        #   • Fallback path in `get_my_attempt`: the fallback query
        #     also carries `.select_related('master_exam')`.
        #
        # If a future caller constructs this serializer from a bare
        # queryset (`.filter(...)` without select_related), that
        # caller is responsible for adding the select_related — the
        # contract is that every serializer instance has a cached
        # `master_exam`.
        return obj.master_exam.duration_minutes

    def get_grace_seconds(self, obj):
        return getattr(settings, 'MASTER_EXAM_GRACE_SECONDS', 180)


class MasterExamAttemptStatusSerializer(serializers.Serializer):
    attempt_id = serializers.IntegerField()
    session_id = serializers.CharField()
    master_exam_id = serializers.IntegerField()
    is_active = serializers.BooleanField()
    is_complete = serializers.BooleanField()
    is_makeup = serializers.BooleanField()
    question_ids = serializers.ListField()
    answers = serializers.DictField()
    current_question_id = serializers.IntegerField(allow_null=True)
    started_at = serializers.DateTimeField()
    deadline_at = serializers.DateTimeField()
    duration_minutes = serializers.IntegerField()
    grace_seconds = serializers.IntegerField()
    server_now = serializers.DateTimeField()


class MasterExamSubmitAnswerSerializer(serializers.Serializer):
    question_id = serializers.IntegerField(min_value=1)
    answer = serializers.IntegerField(min_value=1, max_value=MAX_CHOICES)
    confidence = serializers.BooleanField(required=False, default=True)
    error_reason = serializers.ChoiceField(
        choices=['unknown', 'misread', 'confused', 'guessed'],
        required=False,
        allow_null=True,
        allow_blank=True,
        default=None,
    )


class MasterExamGotoSerializer(serializers.Serializer):
    question_id = serializers.IntegerField(min_value=1)


class MasterExamFlagSerializer(serializers.Serializer):
    question_id = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(required=False, allow_blank=True, default='')