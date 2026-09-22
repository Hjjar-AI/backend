# backend/apps/questions/serializers/case.py
from rest_framework import serializers

from ..models import (
    ClinicalCase,
    Question,
    CASE_STEM_MAX_LENGTH,
)


class CaseSummarySerializer(serializers.ModelSerializer):
    """Compact read-only view of a ClinicalCase embedded on a Question."""
    authored_by_username = serializers.CharField(
        source='authored_by.username', read_only=True, default=None,
    )

    class Meta:
        model = ClinicalCase
        fields = [
            'id', 'uuid', 'key', 'title', 'stem',
            'authored_by', 'authored_by_username',
        ]
        read_only_fields = fields


class CaseStemUpdateSerializer(serializers.Serializer):
    case_stem = serializers.CharField(
        required=True,
        allow_blank=True,
        allow_null=True,
        max_length=CASE_STEM_MAX_LENGTH,
        trim_whitespace=True,
    )

    def validate_case_stem(self, value):
        if value is None:
            return None
        value = value.strip()
        return value or None


class ClinicalCaseSerializer(serializers.ModelSerializer):
    """
    Read/write serializer for the case admin surface.

    `authored_by` is read-only here — authorship of a case is set at
    creation time from the acting user. Editing the case does not
    change who wrote it.

    `question_count` VISIBILITY SCOPING
    -----------------------------------
    The count reports the questions in the case the CALLER can see,
    not the total number of questions attached. Without the scope,
    a caller who can see the case (because they authored one public
    question in it) receives the total across every author — leaking
    the existence and number of other authors' private drafts.

    Two paths feed the count:

      • When the queryset was annotated with `question_count_visible`
        (CaseListView does this, computing the count for every case
        in the page in a single query), read the annotation. This is
        the fast path and the one the list endpoint uses.
      • Otherwise — single-object reads like CaseDetailView.get/put,
        where no annotation is present — fall back to a scoped
        `.count()`. The context's `request` supplies the caller; if
        it is missing (a serializer instantiated outside a request
        cycle), the unscoped `.count()` is the last resort and
        matches the pre-fix behaviour for that edge case only.
    """
    question_count = serializers.SerializerMethodField(read_only=True)
    authored_by_username = serializers.CharField(
        source='authored_by.username', read_only=True, default=None,
    )

    class Meta:
        model = ClinicalCase
        fields = [
            'id', 'uuid', 'key', 'title', 'stem', 'question_count',
            'authored_by', 'authored_by_username',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'uuid',
            'authored_by', 'authored_by_username',
            'created_at', 'updated_at', 'question_count',
        ]

    def get_question_count(self, obj):
        annotated = getattr(obj, 'question_count_visible', None)
        if annotated is not None:
            return annotated

        request = self.context.get('request')
        user = getattr(request, 'user', None) if request else None
        if user is not None and getattr(user, 'is_authenticated', False):
            return (
                Question.objects
                .visible_to(user)
                .filter(case_id=obj.id)
                .count()
            )

        # No request in context — a serializer instantiated outside
        # the request/response cycle (management command, test,
        # ad-hoc shell use). Matches the pre-fix behaviour for this
        # edge case; the API surface always has a request.
        return obj.questions.count()

    def create(self, validated_data):
        request = self.context.get('request')
        if request and getattr(request.user, 'is_authenticated', False):
            validated_data['authored_by'] = request.user
        return super().create(validated_data)