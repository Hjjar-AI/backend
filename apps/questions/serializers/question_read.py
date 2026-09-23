# backend/apps/questions/serializers/question_read.py
from django.db.models import Count
from rest_framework import serializers

from ..models import Question
from .case import CaseSummarySerializer


class QuestionSerializer(serializers.ModelSerializer):
    """
    Read serializer for a Question.

    Authorship is exposed as two independent fields:
      • `authored_by` / `authored_by_username` — historical author
      • `owned_by`   / `owned_by_username`     — current steward

    The frontend uses `owned_by` to decide whether to show the
    "edit" affordance on a card, and `authored_by` for the display
    badge. See the two-FK docstring on `Question` for the full
    rationale.

    `authored_by_rank` is a computed display payload for the author
    rank badge on the question card. It replaces the old
    `created_by_rank` field, which sourced from `created_by_user`.
    """
    category_name = serializers.CharField(source='category.name', read_only=True)
    category_color = serializers.CharField(source='category.color', read_only=True)
    tags = serializers.StringRelatedField(many=True, read_only=True)
    image_url = serializers.SerializerMethodField()
    case_sibling_count = serializers.SerializerMethodField()

    # Authorship and ownership — the FK plus its display string.
    authored_by_username = serializers.CharField(
        source='authored_by.username', read_only=True, default=None,
    )
    owned_by_username = serializers.CharField(
        source='owned_by.username', read_only=True, default=None,
    )

    # Author-rank badge payload (key + localized label). Computed
    # from `authored_by`, so the badge credits the writer, not the
    # current steward. NULL when the question has no author (seed
    # content, or an import whose source author was unresolved).
    authored_by_rank = serializers.SerializerMethodField()

    # Read-only nested case. Write paths use `case_key` / `case_stem`
    # (see create and update serializers).
    case = CaseSummarySerializer(read_only=True)

    class Meta:
        model = Question
        fields = [
            'id', 'uuid',
            'question', 'choices', 'correct_answer', 'explanation', 'source',
            'source_document', 'source_page', 'translations',
            'image_url',
            'tags', 'difficulty', 'category', 'category_name', 'category_color',
            'verified', 'verified_by', 'verified_at', 'verification_notes',
            'authored_by', 'authored_by_username', 'authored_by_rank',
            'owned_by', 'owned_by_username',
            'created_at', 'updated_at', 'updated_by',
            'times_answered', 'times_correct', 'version',
            'case', 'case_order', 'case_sibling_count',
        ]
        read_only_fields = [
            'id', 'uuid',
            'created_at', 'updated_at',
            'updated_by', 'times_answered', 'times_correct', 'version',
            'image_url', 'case_sibling_count',
            'authored_by', 'authored_by_username', 'authored_by_rank',
            'owned_by', 'owned_by_username',
            'case',
        ]

    def get_image_url(self, obj):
        if not obj.image:
            return None
        try:
            return obj.image.url
        except Exception:
            return None

    def get_case_sibling_count(self, obj):
        if not obj.case_id:
            return 0

        cache = self.context.get('case_sibling_cache') if hasattr(self, 'context') else None
        if cache is not None and obj.case_id in cache:
            return cache[obj.case_id]
        return self._count_case_siblings(obj)

    def _count_case_siblings(self, obj):
        request = self.context.get('request') if hasattr(self, 'context') else None
        user = getattr(request, 'user', None) if request is not None else None
        qs = Question.objects.filter(case_id=obj.case_id)
        if user is None or not getattr(user, 'is_authenticated', False):
            qs = qs.public()
        else:
            qs = qs.visible_to(user)
        return qs.count()

    def get_authored_by_rank(self, obj):
        user = obj.authored_by
        if user is None:
            return None
        return {
            'key': user.author_rank,
            'label': user.author_rank_label_ar,
        }

    @staticmethod
    def build_case_sibling_cache(questions, user):
        case_ids = {q.case_id for q in questions if q.case_id}
        if not case_ids:
            return {}
        qs = Question.objects.filter(case_id__in=case_ids)
        if user is None or not getattr(user, 'is_authenticated', False):
            qs = qs.public()
        else:
            qs = qs.visible_to(user)
        counts = qs.values('case_id').annotate(c=Count('id'))
        return {row['case_id']: row['c'] for row in counts}
