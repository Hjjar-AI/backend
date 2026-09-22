# backend/apps/groups/serializers.py
from rest_framework import serializers

from .models import Group, GroupMembership


def _member_count(group):
    annotated = getattr(group, 'member_count', None)
    return annotated if annotated is not None else group.memberships.count()


class GroupMembershipSerializer(serializers.ModelSerializer):
   
    user_id = serializers.IntegerField(read_only=True)
    username = serializers.CharField(source='user.username', read_only=True)
    full_name = serializers.CharField(source='user.full_name', read_only=True)
    current_streak = serializers.IntegerField(source='user.current_streak', read_only=True)
    longest_streak = serializers.IntegerField(source='user.longest_streak', read_only=True)
    role = serializers.CharField(source='user.role', read_only=True)
    is_active = serializers.BooleanField(source='user.is_active', read_only=True)

    class Meta:
        model = GroupMembership
        fields = [
            'id', 'user_id', 'username', 'full_name', 'role', 'is_active',
            'current_streak', 'longest_streak',
            'show_in_leaderboard', 'joined_at',
        ]
        read_only_fields = [
            'id', 'user_id', 'username', 'full_name', 'role', 'is_active',
            'current_streak', 'longest_streak', 'joined_at',
        ]


class GroupSerializer(serializers.ModelSerializer):
  
    member_count = serializers.SerializerMethodField()
    members = GroupMembershipSerializer(source='memberships', many=True, read_only=True)

    class Meta:
        model = Group
        fields = [
            'id', 'name', 'description', 'is_active',
            'created_by', 'created_at', 'updated_at',
            'member_count', 'members',
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at', 'member_count', 'members']

    def get_member_count(self, obj):
        return _member_count(obj)


class GroupListSerializer(serializers.ModelSerializer):

    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Group
        fields = ['id', 'name', 'description', 'member_count']

    def get_member_count(self, obj):
        return _member_count(obj)


class GroupCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    description = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('اسم المجموعة مطلوب')
        if Group.objects.filter(name=value).exists():
            raise serializers.ValidationError('يوجد مجموعة بنفس الاسم')
        return value


class GroupUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('اسم المجموعة مطلوب')
        return value


class GroupAddMembersSerializer(serializers.Serializer):
    user_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )


class GroupVisibilitySerializer(serializers.Serializer):
    show_in_leaderboard = serializers.BooleanField()
