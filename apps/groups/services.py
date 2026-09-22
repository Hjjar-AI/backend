# backend/apps/groups/services.py
from datetime import timedelta

from django.db.models import Count
from django.db import transaction
from django.utils import timezone

from apps.exams.services.activity import group_activity
from apps.users.models import User

from .models import Group, GroupMembership


class GroupService:

    @staticmethod
    def list_groups(include_inactive=False):
        qs = Group.objects.all()
        if not include_inactive:
            qs = qs.filter(is_active=True)
        return qs.annotate(member_count=Count('memberships'))

    @staticmethod
    def create_group(name, description, created_by):
        return Group.objects.create(
            name=name.strip(),
            description=(description or '').strip() or None,
            created_by=created_by,
        )

    @staticmethod
    def update_group(group, name=None, description=None, is_active=None):
        if name is not None:
            group.name = name.strip()
        if description is not None:
            group.description = description.strip() or None
        if is_active is not None:
            group.is_active = bool(is_active)
        group.save()
        return group

    @staticmethod
    def delete_group(group):
        group.delete()

    @staticmethod
    @transaction.atomic
    def add_members(group, user_ids):
        if not user_ids:
            return 0

        Group.objects.select_for_update().get(pk=group.pk)

        seen_ids = set()
        unique_ids = []
        for uid in user_ids:
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
            unique_ids.append(uid)

        existing = set(
            group.memberships
            .filter(user_id__in=unique_ids)
            .values_list('user_id', flat=True)
        )
        to_create = [
            GroupMembership(group=group, user_id=uid)
            for uid in unique_ids
            if uid not in existing
        ]
        if not to_create:
            return 0

        GroupMembership.objects.bulk_create(to_create, ignore_conflicts=True)
        return len(to_create)

    @staticmethod
    def remove_member(group, user_id):
        deleted, _ = group.memberships.filter(user_id=user_id).delete()
        return deleted > 0

    @staticmethod
    def set_visibility(group, user_id, show_in_leaderboard):
        updated = group.memberships.filter(user_id=user_id).update(
            show_in_leaderboard=bool(show_in_leaderboard)
        )
        return updated > 0

    @staticmethod
    def groups_for_user(user):
        return (
            Group.objects
            .filter(is_active=True, memberships__user=user)
            .order_by('name')
        )


class LeaderboardService:

    @staticmethod
    def group_leaderboard(group, days=7):
        # Count both regular sessions and completed master exams.
        cutoff = timezone.now() - timedelta(days=days)
        member_ids = list(
            group.memberships
            .filter(show_in_leaderboard=True)
            .values_list('user_id', flat=True)
        )
        if not member_ids:
            return []
        session_map = group_activity(member_ids, cutoff)

        users = User.objects.filter(id__in=member_ids).only(
            'id', 'username', 'full_name', 'current_streak', 'longest_streak'
        )

        rows = []
        for u in users:
            s = session_map.get(u.id, {})
            answered = s.get('questions_answered') or 0
            correct = s.get('correct') or 0
            rows.append({
                'user_id': u.id,
                'username': u.username,
                'full_name': u.full_name or u.username,
                'questions_answered': answered,
                'correct_count': correct,
                'sessions': s.get('sessions') or 0,
                'accuracy': round((correct / answered) * 100, 1) if answered else 0.0,
                'current_streak': u.current_streak or 0,
                'longest_streak': u.longest_streak or 0,
            })
        rows.sort(key=lambda r: (-r['questions_answered'], -r['accuracy']))
        for idx, r in enumerate(rows, 1):
            r['rank'] = idx

        return rows
