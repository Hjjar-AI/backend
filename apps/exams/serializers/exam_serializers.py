# backend/apps/exams/serializers/exam_serializers.py

from rest_framework import serializers
from django.db import transaction

from ..models import ExamSession, TestHistory, Blueprint, BlueprintWeight


class ExamSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExamSession
        fields = [
            'id', 'session_id', 'user', 'mode', 'question_ids', 'answers',
            'current_index', 'tag', 'started_at', 'accumulated_time',
            'is_active', 'created_at', 'blueprint',
        ]
        read_only_fields = ['id', 'user', 'created_at', 'blueprint']


class TestHistorySerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True, default='محذوف')
    full_name = serializers.CharField(source='user.full_name', read_only=True, default='محذوف')

    class Meta:
        model = TestHistory
        fields = [
            'id', 'user', 'username', 'full_name', 'mode', 'tag',
            'total_questions', 'correct_count', 'accuracy', 'time_spent',
            'completed_at',
        ]
        read_only_fields = ['id', 'user', 'completed_at']


class SessionIdSerializer(serializers.Serializer):
    session_id = serializers.CharField()


class SessionIdOrModeSerializer(serializers.Serializer):
    session_id = serializers.CharField(required=False, allow_blank=True)
    mode = serializers.CharField(required=False, allow_blank=True)


class SubmitAnswerSerializer(serializers.Serializer):
    session_id = serializers.CharField()
    answer = serializers.IntegerField(required=False, allow_null=True, default=None)
    action = serializers.CharField(required=False, default='next')
    target_index = serializers.IntegerField(required=False, allow_null=True, default=None)
    confidence = serializers.BooleanField(required=False, allow_null=True, default=None)

    error_reason = serializers.ChoiceField(
        choices=['unknown', 'misread', 'confused', 'guessed'],
        required=False,
        allow_null=True,
        allow_blank=True,
        default=None,
    )


class BlueprintSerializer(serializers.ModelSerializer):
    """
    Blueprint read/write with a `weights` dict on the wire.

    Read shape:
        {
          "id": 3, "name": "...", "description": "...", "is_active": true,
          "weights": { "1": 2.0, "4": 1.5 },   // category_id → weight
          ...
        }

    Write shape: the same `weights` dict. Zero weights are dropped (an
    absent key is the canonical "category not in this blueprint").
    Category IDs that do not resolve to a real Category are silently
    dropped, matching the previous behaviour.

    The wire shape is deliberately unchanged so the frontend does not
    need a coordinated release.
    """
    weights = serializers.DictField(
        child=serializers.FloatField(),
        required=False,
        default=dict,
    )

    class Meta:
        model = Blueprint
        fields = [
            'id', 'name', 'description', 'weights', 'is_active',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']

    def validate_weights(self, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError('يجب أن تكون الأوزان كائناً (dict)')
        cleaned = {}
        for k, v in value.items():
            try:
                weight = float(v)
            except (TypeError, ValueError):
                raise serializers.ValidationError(f'الوزن لقيمة {k} ليس رقماً صالحاً')
            if weight < 0:
                raise serializers.ValidationError(f'الوزن لقيمة {k} لا يمكن أن يكون سالباً')
            if weight == 0:
                continue
            # Coerce keys to strings here so the downstream write path
            # does not need to worry about int-vs-str key variants.
            cleaned[str(k)] = weight
        return cleaned

    @transaction.atomic
    def create(self, validated_data):
        weights = validated_data.pop('weights', {}) or {}
        blueprint = Blueprint.objects.create(**validated_data)
        self._set_weights(blueprint, weights)
        return blueprint

    @transaction.atomic
    def update(self, instance, validated_data):
        weights = validated_data.pop('weights', None)
        for attr, val in validated_data.items():
            setattr(instance, attr, val)
        instance.save()
        # Only touch the through table when the caller actually sent
        # a weights payload. A partial update that only changes
        # `is_active` must not silently clear the weights.
        if weights is not None:
            self._set_weights(instance, weights)
        return instance

    @staticmethod
    def _set_weights(blueprint, weights):
        """
        Replace the blueprint's weights with the given dict.

        Zero-weight entries and unresolvable category ids are dropped.
        The replacement is a single transaction: delete existing rows,
        bulk-create new ones.
        """
        from django.db import transaction
        from apps.questions.models import Category

        entries_to_create = []
        if weights:
            # Resolve which category ids actually exist so a stray
            # integer in the payload does not raise IntegrityError.
            requested_ids = []
            for k in weights.keys():
                try:
                    requested_ids.append(int(k))
                except (TypeError, ValueError):
                    continue
            valid_ids = set(
                Category.objects
                .filter(id__in=requested_ids)
                .values_list('id', flat=True)
            )
            for k, v in weights.items():
                try:
                    cid = int(k)
                except (TypeError, ValueError):
                    continue
                if cid not in valid_ids:
                    continue
                entries_to_create.append(
                    BlueprintWeight(
                        blueprint=blueprint,
                        category_id=cid,
                        weight=float(v),
                    )
                )

        with transaction.atomic():
            blueprint.weight_entries.all().delete()
            if entries_to_create:
                BlueprintWeight.objects.bulk_create(entries_to_create)
