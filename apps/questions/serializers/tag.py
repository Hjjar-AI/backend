# backend/apps/questions/serializers/tag.py
from rest_framework import serializers

from ..models import Tag


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ['id', 'uuid', 'name', 'parent']
        read_only_fields = ['id', 'uuid']


# ── Rename / merge name fields ────────────────────────────────────────
#
# NOTE ON THE ABSENT `max_length`
# -------------------------------
# The two `CharField`s below are declared WITHOUT a `max_length`
# argument, deliberately. The length rule for a tag name lives in
# `apps.questions.models.TAG_NAME_MAX_LENGTH` and is enforced at the
# VIEW layer by `_validate_tag_name`, which raises the app's own
# Arabic message ("اسم الوسم يجب ألا يتجاوز N حرفاً").
#
# If `max_length` were declared on the serializer field, DRF would
# short-circuit an over-long name before the view was reached and
# emit its own English message ("Ensure this field has no more than
# N characters"). The caller would then see a different message for
# the same rule depending on which code path caught them — a
# needless inconsistency given the codebase's otherwise uniform
# error wording.
#
# Do NOT add `max_length` to these fields. The view-layer check is
# the one authoritative rule; DRF's implicit one is intentionally
# disabled.


class TagRenameSerializer(serializers.Serializer):
    new_name = serializers.CharField(required=False, allow_blank=True, default='')


class TagMergeSerializer(serializers.Serializer):
    source_tags = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    target_tag = serializers.CharField(required=False, allow_blank=True, default='')