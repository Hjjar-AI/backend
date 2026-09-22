"""Field builders shared by public-question and master-draft writers."""

from rest_framework import serializers

from .models import (
    QUESTION_TEXT_MAX_LENGTH, CHOICE_TEXT_MAX_LENGTH,
    CASE_GROUP_MAX_LENGTH, CASE_STEM_MAX_LENGTH,
)

_UNSET = object()


def question_text_field(required=True):
    return serializers.CharField(
        max_length=QUESTION_TEXT_MAX_LENGTH,
        allow_blank=False, trim_whitespace=True, required=required,
    )


def choices_field(max_choices, *, required=True, write_only=False,
                  enforce_child_length=False):
    child_kwargs = {'allow_blank': False}
    if enforce_child_length:
        child_kwargs['max_length'] = CHOICE_TEXT_MAX_LENGTH
    return serializers.ListField(
        child=serializers.CharField(**child_kwargs),
        allow_empty=False, min_length=2, max_length=max_choices,
        required=required, write_only=write_only,
    )


def case_key_field(default=_UNSET):
    kwargs = {}
    if default is not _UNSET:
        kwargs['default'] = default
    return serializers.CharField(
        required=False, allow_blank=True, allow_null=True,
        max_length=CASE_GROUP_MAX_LENGTH, trim_whitespace=True, **kwargs,
    )


def case_stem_field(default=_UNSET):
    kwargs = {}
    if default is not _UNSET:
        kwargs['default'] = default
    return serializers.CharField(
        required=False, allow_blank=True, allow_null=True,
        max_length=CASE_STEM_MAX_LENGTH, trim_whitespace=True, **kwargs,
    )


def case_order_field(default=_UNSET):
    kwargs = {}
    if default is not _UNSET:
        kwargs['default'] = default
    return serializers.IntegerField(
        required=False, allow_null=True, min_value=1, **kwargs,
    )


def normalize_case_fields(attrs):
    for key in ('case_key', 'case_stem'):
        if key in attrs and attrs[key] is not None:
            attrs[key] = attrs[key].strip() or None
    return attrs
