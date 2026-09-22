# backend/apps/master_exams/serializers/acknowledgement.py
"""
Acknowledgement serializers.

`MasterExamAcknowledgeSerializer` is a no-body serializer; the exam
id comes from the URL. `MasterExamAcknowledgementSerializer` is the
read model for the acknowledgement rows.
"""

from rest_framework import serializers

from ..models import MasterExamAcknowledgement


class MasterExamAcknowledgeSerializer(serializers.Serializer):
    """No body fields; the exam id comes from the URL."""


class MasterExamAcknowledgementSerializer(serializers.ModelSerializer):
    exam_name = serializers.CharField(source='master_exam.name', read_only=True)

    class Meta:
        model = MasterExamAcknowledgement
        fields = ['id', 'master_exam', 'exam_name', 'acknowledged_at']
        read_only_fields = fields