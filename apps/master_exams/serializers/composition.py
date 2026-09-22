# backend/apps/master_exams/serializers/composition.py
"""
Composition serializers for add / remove / reorder question
operations on an exam.

OPTIMISTIC LOCKING (expected_version)
-------------------------------------
Each write serializer now accepts an optional `expected_version`
integer. When supplied, the corresponding service function CASes
`MasterExam.version` before touching the through table and rejects a
stale read with 409 MODIFIED_BY_ANOTHER_USER. When omitted, the write
still proceeds and the version still advances — see the service
docstrings for the mixed-deployment rationale.

The field is optional on purpose. A caller that does not yet know
about it (an older frontend build, a script) keeps working exactly as
before; a caller that does know about it gains conflict protection
for its own edit and gets a conflict signal when another writer has
moved the version since its read.
"""

from rest_framework import serializers


class MasterExamAddQuestionsSerializer(serializers.Serializer):
    question_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )
    expected_version = serializers.IntegerField(required=False, allow_null=True)


class MasterExamRemoveQuestionSerializer(serializers.Serializer):
    question_id = serializers.IntegerField(min_value=1)
    expected_version = serializers.IntegerField(required=False, allow_null=True)


class MasterExamReorderSerializer(serializers.Serializer):
    question_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=True,
    )
    expected_version = serializers.IntegerField(required=False, allow_null=True)