# backend/apps/master_exams/serializers/draft.py
"""
Draft-question serializers.

`MasterExamDraftSerializer` is the read side and reads a `Question`
(not a `MasterExam`) — drafts are a question-library concept.
`MasterExamDraftCreateSerializer` is the write side and mirrors
`QuestionCreateSerializer`'s validation, including the case block.
"""

from rest_framework import serializers

from apps.questions.models import Question
from apps.questions.validation import clean_and_validate_choices
from apps.questions.write_fields import (
    question_text_field, choices_field, case_key_field,
    case_stem_field, case_order_field, normalize_case_fields,
)

from ._constants import MAX_CHOICES


class MasterExamDraftSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    category_color = serializers.CharField(source='category.color', read_only=True)
    image_url = serializers.SerializerMethodField()
    tags = serializers.StringRelatedField(many=True, read_only=True)
    case = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            'id', 'question', 'choices', 'correct_answer', 'explanation',
            'source', 'image_url', 'tags', 'difficulty',
            'category', 'category_name', 'category_color',
            'is_draft', 'draft_owner', 'created_at', 'updated_at',
            'case', 'case_order',
        ]
        read_only_fields = fields

    def get_image_url(self, obj):
        if not obj.image:
            return None
        try:
            return obj.image.url
        except Exception:
            return None

    def get_case(self, obj):
        if not obj.case_id:
            return None
        return {
            'id': obj.case.id,
            'key': obj.case.key,
            'title': obj.case.title,
            'stem': obj.case.stem,
        }


class MasterExamDraftCreateSerializer(serializers.Serializer):
    """
    Write-side serializer for a draft question.

    The case block mirrors QuestionCreateSerializer:
      • `case_key`   — attaches / detaches / moves the draft's case.
                       Null or "" detaches.
      • `case_stem`  — optional vignette. Applied only on case
                       creation or when the target case has no stem
                       yet. See _resolve_case for the fill rules.
      • `case_order` — position within the case.
    """
    question = question_text_field()
    choices = choices_field(MAX_CHOICES, enforce_child_length=True)
    correct_answer = serializers.IntegerField(min_value=1)
    explanation = serializers.CharField(required=False, allow_blank=True, default='')
    source = serializers.CharField(required=False, allow_blank=True, default='', max_length=200)
    difficulty = serializers.ChoiceField(
        choices=Question.DIFFICULTY_CHOICES,
        required=False,
        default='medium',
    )
    category = serializers.IntegerField(required=False, allow_null=True, default=None)

    case_key = case_key_field(default=None)
    case_stem = case_stem_field(default=None)
    case_order = case_order_field(default=None)

    def validate(self, attrs):
        # Shared validation — see apps/questions/validation.py.
        # This serializer's rules were a strict subset of
        # QuestionCreateSerializer's (same messages, same order);
        # both now delegate to the same function.
        cleaned, error = clean_and_validate_choices(
            attrs['choices'],
            attrs['correct_answer'],
            max_choices=MAX_CHOICES,
        )
        if error is not None:
            if error['field']:
                raise serializers.ValidationError({error['field']: error['message']})
            raise serializers.ValidationError(error['message'])

        attrs['choices'] = cleaned

        return normalize_case_fields(attrs)
