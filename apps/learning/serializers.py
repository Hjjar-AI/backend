# backend/apps/learning/serializers.py

from rest_framework import serializers

from .models import UserQuestionAttempt


class UserQuestionAttemptSerializer(serializers.ModelSerializer):
    question_id = serializers.IntegerField(read_only=True)
    next_due = serializers.DateTimeField(read_only=True)
    last_answered_at = serializers.DateTimeField(read_only=True)

    class Meta:
        model = UserQuestionAttempt
        fields = [
            'question_id',
            'last_correct', 'last_confidence', 'last_error_reason',
            'last_answered_at',
            'attempts', 'wrong_count', 'ever_correct',
            'ease_factor', 'interval_days', 'repetitions', 'next_due',
        ]
        read_only_fields = fields