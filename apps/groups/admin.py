# backend/apps/groups/admin.py
from django.contrib import admin

from .models import Group, GroupMembership


class GroupMembershipInline(admin.TabularInline):
    model = GroupMembership
    extra = 0
    autocomplete_fields = ['user']
    fields = ('user', 'show_in_leaderboard', 'joined_at')
    readonly_fields = ('joined_at',)


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'member_count', 'created_by', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'description')
    inlines = [GroupMembershipInline]

    def member_count(self, obj):
        return obj.memberships.count()
    member_count.short_description = 'Members'


@admin.register(GroupMembership)
class GroupMembershipAdmin(admin.ModelAdmin):
    list_display = ('group', 'user', 'show_in_leaderboard', 'joined_at')
    list_filter = ('group', 'show_in_leaderboard')
    search_fields = ('user__username', 'group__name')
    autocomplete_fields = ['user', 'group']