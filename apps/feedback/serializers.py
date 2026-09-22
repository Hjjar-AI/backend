# backend/apps/feedback/serializers.py

from rest_framework import serializers

from .models import Bookmark, QuestionFlag, QuestionRating


class BookmarkSerializer(serializers.ModelSerializer):
    class Meta:
        model = Bookmark
        fields = ['id', 'user', 'question', 'created_at']


class FlagSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuestionFlag
        fields = ['id', 'question', 'user', 'reason', 'created_at', 'resolved', 'resolved_by', 'resolved_at']
        read_only_fields = ['user', 'resolved', 'resolved_by', 'resolved_at']


class RatingSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuestionRating
        fields = ['id', 'question', 'user', 'rating', 'created_at', 'updated_at']
        read_only_fields = ['user', 'created_at', 'updated_at']


class FlagQuestionSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class RateQuestionSerializer(serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5)