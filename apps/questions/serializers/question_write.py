# backend/apps/questions/serializers/question_write.py
from rest_framework import serializers

from ..models import (
    Question,
    Tag,
    clean_tag_name,
    EXPLANATION_TEXT_MAX_LENGTH,
)
from ..validation import (
    clean_and_validate_choices,
    validate_correct_answer,
)
from ..translation_validation import normalize_translations
from .constants import MAX_CHOICES
from .case_resolver import _resolve_case
from ..write_fields import (
    question_text_field, choices_field, case_key_field,
    case_stem_field, case_order_field, normalize_case_fields,
)


class _QuestionWriteFields(serializers.ModelSerializer):
    """Shared field schema; create overrides only its required fields."""
    tags = serializers.CharField(required=False, allow_blank=True)
    question = question_text_field(required=False)
    explanation = serializers.CharField(
        max_length=EXPLANATION_TEXT_MAX_LENGTH, required=False,
        allow_blank=True, allow_null=True,
    )
    choices = choices_field(MAX_CHOICES, required=False)
    source_page = serializers.IntegerField(
        min_value=1, required=False, allow_null=True,
    )
    case_key = case_key_field()
    case_stem = case_stem_field()
    case_order = case_order_field()

    class Meta:
        model = Question
        fields = [
            'question', 'choices', 'correct_answer', 'explanation', 'source',
            'source_document', 'source_page', 'translations',
            'tags', 'difficulty', 'category',
            'case_key', 'case_stem', 'case_order',
        ]

    def validate_translations(self, value):
        try:
            return normalize_translations(value, max_choices=MAX_CHOICES)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class QuestionCreateSerializer(_QuestionWriteFields):
    """Create a question; authorship and ownership come from the service."""
    question = question_text_field()
    choices = choices_field(MAX_CHOICES, write_only=True)
    case_key = case_key_field(default=None)
    case_stem = case_stem_field(default=None)
    case_order = case_order_field(default=None)

    def validate(self, attrs):
        # Shared validation — see apps/questions/validation.py.
        #
        # ERROR SHAPE: every failure here is raised as a plain
        # string, matching the pre-refactor behaviour of this
        # serializer. The shared helper tags the correct-answer
        # failure with `field='correct_answer'`, but this serializer
        # deliberately ignores that hint — the original code raised
        # a plain string for this case, and changing the shape would
        # change the JSON `details` object the frontend receives.
        cleaned, error = clean_and_validate_choices(
            attrs.get('choices', []),
            attrs.get('correct_answer'),
            max_choices=MAX_CHOICES,
        )
        if error is not None:
            raise serializers.ValidationError(error['message'])

        attrs['choices'] = cleaned

        return normalize_case_fields(attrs)

    def create(self, validated_data):
        tags_str = validated_data.pop('tags', '')
        case_key = validated_data.pop('case_key', None)
        case_stem = validated_data.pop('case_stem', None)

        # The FK to the authored user is supplied as a kwarg by the
        # service layer (`QuestionService.create_question` passes
        # `authored_by=user, owned_by=user`). Pull it out here so
        # `_resolve_case` can attribute the case consistently.
        authored_by = validated_data.get('authored_by')
        case = _resolve_case(case_key, authored_by, stem=case_stem)

        if case is not None:
            validated_data['case'] = case

        question = Question.objects.create(**validated_data)

        if tags_str:
            tags = [t.strip() for t in tags_str.split(',') if t.strip()]
            for tag_name in tags:
                tag, _ = Tag.objects.get_or_create(name=clean_tag_name(tag_name))
                question.tags.add(tag)

        return question


class QuestionUpdateSerializer(_QuestionWriteFields):
    """
    Update a question without exposing author or owner FKs.

    EXPECTED VERSION (optimistic locking)
    -------------------------------------
    `expected_version` is the client's snapshot of `Question.version`.
    It is optional. When supplied, `QuestionService.update_question`
    CASes the version column before writing and rejects a stale value
    with a 409 ("modified by another user"). When omitted, the write
    proceeds unconditionally and the version is still bumped.

    Declared here rather than on `_QuestionWriteFields` so that the
    create serializer does not accept a key it has no use for — the
    create path does not perform a version check, and an inert
    `expected_version` in the create payload would be a silent trap
    for a future reader who assumes every write path honours it.
    """

    expected_version = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = Question
        fields = _QuestionWriteFields.Meta.fields + ['expected_version']

    def validate(self, attrs):
        # Full validation runs only when the caller actually sends a
        # new choices list. A partial update that touches only, say,
        # `difficulty` must not require the caller to resend every
        # choice — see the pre-refactor behaviour this preserves.
        #
        # ERROR SHAPE: like the create serializer, this one raises
        # every failure as a plain string. The shared helper's
        # `field='correct_answer'` hint is deliberately ignored here
        # so the DRF response shape matches the original.
        #
        # SINGLE SOURCE OF TRUTH (fix — validation.py owns the message)
        # -------------------------------------------------------------
        # The `elif` branch below (no new choices, but a new
        # correct_answer) used to hand-write the same
        # "رقم الإجابة الصحيحة يجب أن يكون بين 1 و {n}" string that
        # `validate_correct_answer` produces. That made this file the
        # third place the message lived. It now calls the shared
        # helper.
        if 'choices' in attrs:
            correct = attrs.get(
                'correct_answer',
                self.instance.correct_answer if self.instance else None,
            )
            cleaned, error = clean_and_validate_choices(
                attrs['choices'],
                correct,
                max_choices=MAX_CHOICES,
            )
            if error is not None:
                raise serializers.ValidationError(error['message'])

            attrs['choices'] = cleaned

        elif self.instance and 'correct_answer' in attrs:
            # No new choices — the correct-answer index must still
            # fall inside the EXISTING list.
            existing_len = (
                len(self.instance.choices) if self.instance.choices else 0
            )
            range_error = validate_correct_answer(
                attrs['correct_answer'], existing_len,
            )
            if range_error is not None:
                raise serializers.ValidationError(range_error['message'])

        return normalize_case_fields(attrs)
