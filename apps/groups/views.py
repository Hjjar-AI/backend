# backend/apps/groups/views.py

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404

from .models import Group, GroupMembership
from .serializers import (
    GroupSerializer,
    GroupListSerializer,
    GroupCreateSerializer,
    GroupUpdateSerializer,
    GroupAddMembersSerializer,
    GroupVisibilitySerializer,
)
from .services import GroupService, LeaderboardService
from apps.core.permissions import HasCapability
from apps.core.utils import api_success, api_error
from apps.core.audit import log_privileged_action
from apps.users.models import User


class MyGroupsView(APIView):
    """
    Groups the caller is a member of. Read-only.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = GroupService.groups_for_user(request.user)
        serializer = GroupListSerializer(qs, many=True)
        return api_success(data={'items': serializer.data})


class GroupLeaderboardView(APIView):
    """
    Leaderboard for one group. A caller without membership in the
    group may only see it if they hold 'groups.admin' — an
    administrator can inspect any group without joining it.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, group_id):
        group = get_object_or_404(Group, id=group_id, is_active=True)

        is_member = GroupMembership.objects.filter(
            group=group, user=request.user,
        ).exists()
        if not is_member and not request.user.has_capability('groups.admin'):
            # Same 404 as a nonexistent group so membership cannot be
            # probed by observing 403 vs 404.
            return api_error('المجموعة غير موجودة', 404)

        try:
            days = int(request.query_params.get('days', 7))
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(days, 90))

        rows = LeaderboardService.group_leaderboard(group, days=days)
        return api_success(data={
            'group': {
                'id': group.id,
                'name': group.name,
                'description': group.description,
            },
            'days': days,
            'rows': rows,
        })


class GroupVisibilityView(APIView):
    """
    Toggle the caller's own appearance in a group leaderboard.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, group_id):
        group = get_object_or_404(Group, id=group_id)
        body = GroupVisibilitySerializer(data=request.data)
        if not body.is_valid():
            return api_error('بيانات غير صالحة', 400, details=body.errors)

        ok = GroupService.set_visibility(
            group,
            request.user.id,
            body.validated_data['show_in_leaderboard'],
        )
        if not ok:
            return api_error('أنت لست عضواً في هذه المجموعة', 404)
        return api_success(message='تم تحديث إعداد الظهور')


class AdminGroupListView(APIView):
    """
    Group CRUD, list and create. Requires 'groups.admin'.
    """
    permission_classes = [HasCapability]
    required_capability = 'groups.admin'

    def get(self, request):
        include_inactive = request.query_params.get('include_inactive') in (
            '1', 'true', 'True',
        )
        qs = GroupService.list_groups(include_inactive=include_inactive)
        serializer = GroupSerializer(qs, many=True)
        return api_success(data={
            'items': serializer.data,
            'total': qs.count(),
        })

    def post(self, request):
        body = GroupCreateSerializer(data=request.data)
        if not body.is_valid():
            return api_error('بيانات غير صالحة', 400, details=body.errors)
        group = GroupService.create_group(
            body.validated_data['name'],
            body.validated_data.get('description', ''),
            request.user.username,
        )
        log_privileged_action(request, 'group.create', target=group)
        return api_success(data=GroupSerializer(group).data, code=201)


class AdminGroupDetailView(APIView):
    """
    Group CRUD, detail. Requires 'groups.admin'.
    """
    permission_classes = [HasCapability]
    required_capability = 'groups.admin'

    def get(self, request, group_id):
        group = get_object_or_404(Group, id=group_id)
        return api_success(data=GroupSerializer(group).data)

    def put(self, request, group_id):
        group = get_object_or_404(Group, id=group_id)
        body = GroupUpdateSerializer(data=request.data)
        if not body.is_valid():
            return api_error('بيانات غير صالحة', 400, details=body.errors)

        new_name = body.validated_data.get('name')
        if new_name and Group.objects.filter(name=new_name).exclude(id=group.id).exists():
            return api_error('يوجد مجموعة بنفس الاسم', 400)

        group = GroupService.update_group(
            group,
            name=body.validated_data.get('name'),
            description=body.validated_data.get('description'),
            is_active=body.validated_data.get('is_active'),
        )
        log_privileged_action(request, 'group.update', target=group)
        return api_success(data=GroupSerializer(group).data)

    def delete(self, request, group_id):
        group = get_object_or_404(Group, id=group_id)
        group_name = group.name
        GroupService.delete_group(group)
        log_privileged_action(request, 'group.delete', target_repr=group_name,
                              details={'group_id': group_id})
        return api_success(message='تم حذف المجموعة')


class AdminGroupMembersView(APIView):
    """
    Bulk add members. Requires 'groups.admin'.

    Invalid user ids are silently dropped — the endpoint reports how
    many of the requested ids were actually valid, and how many were
    newly added. Existing memberships are not errors.
    """
    permission_classes = [HasCapability]
    required_capability = 'groups.admin'

    def post(self, request, group_id):
        group = get_object_or_404(Group, id=group_id)
        body = GroupAddMembersSerializer(data=request.data)
        if not body.is_valid():
            return api_error('بيانات غير صالحة', 400, details=body.errors)

        valid_ids = set(
            User.objects
            .filter(id__in=body.validated_data['user_ids'])
            .values_list('id', flat=True)
        )
        if not valid_ids:
            return api_error('لم يتم العثور على مستخدمين صالحين', 400)

        added = GroupService.add_members(group, list(valid_ids))
        if added:
            log_privileged_action(request, 'group.members_add', target=group,
                                  details={'added': added})
        return api_success(
            data={
                'added': added,
                'requested': len(body.validated_data['user_ids']),
            },
            message=f'تمت إضافة {added} عضو',
        )


class AdminGroupMemberView(APIView):
    """
    Remove one member. Requires 'groups.admin'.
    """
    permission_classes = [HasCapability]
    required_capability = 'groups.admin'

    def delete(self, request, group_id, user_id):
        group = get_object_or_404(Group, id=group_id)
        ok = GroupService.remove_member(group, user_id)
        if not ok:
            return api_error('العضو غير موجود في المجموعة', 404)
        log_privileged_action(request, 'group.member_remove', target=group,
                              details={'user_id': user_id})
        return api_success(message='تمت إزالة العضو')
