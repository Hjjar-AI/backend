# backend/apps/questions/serializers/bulk.py
from rest_framework import serializers


class QuestionBatchSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=list,
    )


class BulkVerifySerializer(serializers.Serializer):
    question_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=list,
    )
    action = serializers.ChoiceField(
        choices=['verify', 'unverify'],
        required=False,
        default='verify',
    )
    verification_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
    )


class BulkTagUpdateSerializer(serializers.Serializer):
    question_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=list,
    )
    add_tags = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    remove_tags = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )