# backend/apps/master_exams/serializers/exam_crud.py
"""
Exam CRUD serializers.

List / detail read side and create / update / delete write side.

`MasterExamListSerializer.get_my_attempt` reads the caller's own
attempt. The list views pass a
`Prefetch(..., to_attr='_my_attempts_prefetched')` so this method
reads from the already-hydrated list; the fallback point query is
used only for create/update detail responses that do not annotate
the queryset.

AUDIENCE COUNT SEMANTICS
------------------------
`get_audience_count` returns the number of audience TARGETING
RULES that have been configured for the exam — the count of
directly-assigned users plus the count of assigned groups. It is
NOT the number of people who can see the exam:

  • An assigned group with 200 members contributes 1 to this
    count, not 200.
  • A user who is in the `audience_all_doctors` population (see
    `MasterExamService.resolve_audience`) contributes 0 to this
    count, because there is no per-user row to count — the targeting
    rule is a boolean flag, not a list.

The accurate name for this field would be `audience_rule_count`,
but the frontend already reads `audience_count` and a rename would
be a coordinated release. The docstring is the load-bearing
contract: any code that wants the actual participant count must
call `MasterExamService.resolve_audience(exam).count()` (used by
`MasterExamResultsService.live_summary`'s `total_assigned`), not
read this field.
"""

from django.conf import settings
from rest_framework import serializers

from ..models import MasterExam, MasterExamAttempt
from .attempt import MasterExamAttemptSerializer


def _question_count(obj):
    annotated = getattr(obj, '_exam_question_count', None)
    return annotated if annotated is not None else obj.exam_questions.count()


def _is_owner(serializer, obj):
    request = serializer.context.get('request')
    if not request or not getattr(request.user, 'is_authenticated', False):
        return False
    user = request.user
    return (
        user.is_admin
        or obj.primary_attending_id == user.id
        or obj.co_attendings.filter(id=user.id).exists()
    )


def _validate_window(opens, closes, duration):
    if opens and closes and opens >= closes:
        raise serializers.ValidationError({
            'closes_at': 'تاريخ الإغلاق يجب أن يكون بعد تاريخ الفتح.'
        })
    if opens and closes and duration:
        window_minutes = int((closes - opens).total_seconds() // 60)
        if duration > window_minutes:
            raise serializers.ValidationError({
                'duration_minutes': (
                    f'مدة الامتحان ({duration} دقيقة) أطول من نافذة '
                    f'الامتحان ({window_minutes} دقيقة).'
                )
            })


class MasterExamListSerializer(serializers.ModelSerializer):
    primary_attending_name = serializers.CharField(
        source='primary_attending.full_name', read_only=True,
    )
    primary_attending_username = serializers.CharField(
        source='primary_attending.username', read_only=True,
    )
    status = serializers.CharField(source='computed_status', read_only=True)
    question_count = serializers.SerializerMethodField()
    co_attending_count = serializers.SerializerMethodField()
    audience_count = serializers.SerializerMethodField()
    is_owner = serializers.SerializerMethodField()
    my_attempt = serializers.SerializerMethodField()

    class Meta:
        model = MasterExam
        fields = [
            'id', 'name', 'description',
            'primary_attending', 'primary_attending_name', 'primary_attending_username',
            'co_attending_count', 'audience_count',
            'opens_at', 'closes_at', 'duration_minutes',
            'question_count', 'status', 'version',
            'allow_makeup', 'shuffle_questions', 'shuffle_choices',
            'is_owner',
            'my_attempt',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def get_question_count(self, obj):
        return _question_count(obj)

    def get_co_attending_count(self, obj):
        annotated = getattr(obj, '_co_attending_count', None)
        if annotated is not None:
            return annotated
        return obj.co_attendings.count()

    def get_audience_count(self, obj):
        """
        Count of audience TARGETING RULES, not people. See the module
        docstring for the full semantics. The real participant count
        is `MasterExamService.resolve_audience(exam).count()` and is
        exposed separately by the results dashboard.
        """
        user_count = getattr(obj, '_audience_user_count', None)
        group_count = getattr(obj, '_audience_group_count', None)
        if user_count is not None and group_count is not None:
            return user_count + group_count
        return obj.audience_users.count() + obj.audience_groups.count()

    def get_is_owner(self, obj):
        return _is_owner(self, obj)

    def get_my_attempt(self, obj):
        """
        Return the caller's own attempt for this exam, or None.

        Prefer the prefetched list supplied by the view.
        `crud_views.py` attaches a `_my_attempts_prefetched`
        attribute via `Prefetch(..., to_attr=...)`, filtered by the
        requesting user and ordered by `-started_at`, so the first
        element is the latest attempt with zero additional queries.

        The fallback path is a single query and is used by
        create/update detail responses that do not annotate the
        queryset — those are single-object responses where one extra
        query is acceptable.
        """
        request = self.context.get('request')
        if not request or not getattr(request.user, 'is_authenticated', False):
            return None

        prefetched = getattr(obj, '_my_attempts_prefetched', None)
        if prefetched is not None:
            attempt = prefetched[0] if prefetched else None
        else:
            attempt = (
                MasterExamAttempt.objects
                .filter(master_exam=obj, user=request.user)
                .select_related('master_exam')
                .order_by('-started_at')
                .first()
            )

        if attempt is None:
            return None
        return MasterExamAttemptSerializer(attempt).data


class MasterExamDetailSerializer(serializers.ModelSerializer):
    status = serializers.CharField(source='computed_status', read_only=True)
    primary_attending_name = serializers.CharField(
        source='primary_attending.full_name', read_only=True,
    )
    co_attendings = serializers.SerializerMethodField()
    audience_groups = serializers.SerializerMethodField()
    audience_users = serializers.SerializerMethodField()
    question_count = serializers.SerializerMethodField()
    question_ids = serializers.SerializerMethodField()
    can_edit_now = serializers.BooleanField(read_only=True)
    can_be_deleted = serializers.BooleanField(read_only=True)
    is_owner = serializers.SerializerMethodField()

    class Meta:
        model = MasterExam
        fields = [
            'id', 'name', 'description', 'instructions',
            'primary_attending', 'primary_attending_name',
            'co_attendings',
            'opens_at', 'closes_at', 'duration_minutes',
            'question_ids', 'question_count',
            'audience_all_doctors', 'audience_groups', 'audience_users',
            'weight_easy', 'weight_medium', 'weight_hard',
            'shuffle_questions', 'shuffle_choices', 'allow_makeup',
            'exam_topic_tag',
            'stored_status', 'status', 'version',
            'published_at', 'completed_at', 'published_to_bank_at',
            'created_at', 'updated_at',
            'can_edit_now', 'can_be_deleted', 'is_owner',
        ]
        read_only_fields = [
            'id', 'primary_attending', 'stored_status', 'status',
            'published_at', 'completed_at', 'published_to_bank_at',
            'created_at', 'updated_at', 'version', 'is_owner',
            'question_ids',
        ]

    def get_is_owner(self, obj):
        return _is_owner(self, obj)

    def get_co_attendings(self, obj):
        return [
            {'id': u.id, 'username': u.username, 'full_name': u.full_name}
            for u in obj.co_attendings.all()
        ]

    def get_audience_groups(self, obj):
        return [
            {'id': g.id, 'name': g.name, 'member_count': g.memberships.count()}
            for g in obj.audience_groups.all()
        ]

    def get_audience_users(self, obj):
        return [
            {'id': u.id, 'username': u.username, 'full_name': u.full_name}
            for u in obj.audience_users.all()
        ]

    def get_question_count(self, obj):
        return _question_count(obj)

    def get_question_ids(self, obj):
        return list(
            obj.exam_questions
            .order_by('order')
            .values_list('question_id', flat=True)
        )


class MasterExamCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    instructions = serializers.CharField(required=False, allow_blank=True, default='')
    opens_at = serializers.DateTimeField()
    closes_at = serializers.DateTimeField()
    duration_minutes = serializers.IntegerField(min_value=1, max_value=600)
    question_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
    )
    audience_all_doctors = serializers.BooleanField(required=False, default=False)
    audience_group_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
    )
    audience_user_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
    )
    co_attending_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
    )
    weight_easy = serializers.FloatField(required=False, default=1.0, min_value=0.1, max_value=10)
    weight_medium = serializers.FloatField(required=False, default=1.0, min_value=0.1, max_value=10)
    weight_hard = serializers.FloatField(required=False, default=1.0, min_value=0.1, max_value=10)
    shuffle_questions = serializers.BooleanField(required=False, default=True)
    shuffle_choices = serializers.BooleanField(required=False, default=False)
    allow_makeup = serializers.BooleanField(required=False, default=True)
    exam_topic_tag = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=100,
    )

    def validate(self, attrs):
        opens = attrs.get('opens_at')
        closes = attrs.get('closes_at')
        duration = attrs.get('duration_minutes')

        _validate_window(opens, closes, duration)

        qids = attrs.get('question_ids') or []
        cap = getattr(settings, 'MASTER_EXAM_MAX_QUESTIONS', 200)
        if len(qids) > cap:
            raise serializers.ValidationError({
                'question_ids': f'الحد الأقصى {cap} سؤال.'
            })

        if qids:
            seen = set()
            unique = []
            for qid in qids:
                if qid in seen:
                    continue
                seen.add(qid)
                unique.append(qid)
            attrs['question_ids'] = unique

        return attrs


class MasterExamUpdateSerializer(MasterExamCreateSerializer):
    name = serializers.CharField(max_length=200, required=False)
    opens_at = serializers.DateTimeField(required=False)
    closes_at = serializers.DateTimeField(required=False)
    duration_minutes = serializers.IntegerField(required=False, min_value=1, max_value=600)
    expected_version = serializers.IntegerField()

    def to_internal_value(self, data):
        if 'question_ids' in data:
            raise serializers.ValidationError({
                'question_ids': (
                    'لتغيير قائمة الأسئلة، استخدم نقاط النهاية المخصصة '
                    '(add / remove / reorder).'
                )
            })
        return super().to_internal_value(data)

    def validate(self, attrs):
        exam = self.context.get('exam')
        opens = attrs.get('opens_at')
        closes = attrs.get('closes_at')
        duration = attrs.get('duration_minutes')

        if exam is not None:
            if opens is None:
                opens = exam.opens_at
            if closes is None:
                closes = exam.closes_at
            if duration is None:
                duration = exam.duration_minutes

        _validate_window(opens, closes, duration)

        return attrs


class MasterExamDeleteSerializer(serializers.Serializer):
    delete_mode = serializers.ChoiceField(
        choices=['delete_drafts', 'keep_drafts', 'publish_drafts'],
    )