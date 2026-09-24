# backend/apps/exams/admin.py
from django.contrib import admin

from .models import (
    ExamSession,
    TestHistory,
    Blueprint,
    BlueprintWeight,
)


@admin.register(ExamSession)
class ExamSessionAdmin(admin.ModelAdmin):
    list_display = ('session_id', 'user', 'mode', 'is_active', 'current_index', 'created_at')
    list_filter = ('mode', 'is_active')
    search_fields = ('session_id', 'user__username')


@admin.register(TestHistory)
class TestHistoryAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'mode', 'tag', 'total_questions', 'answered_count', 'correct_count',
        'accuracy', 'started_at', 'completed_at',
    )
    list_filter = ('mode', 'tag')
    search_fields = ('user__username',)
    date_hierarchy = 'completed_at'
    ordering = ('-completed_at',)
    readonly_fields = (
        'user', 'mode', 'tag',
        'total_questions', 'answered_count', 'correct_count', 'accuracy', 'time_spent',
        'started_at', 'completed_at',
    )

    def has_add_permission(self, request):
        # History is written by ExamService.finish_session, never by
        # hand. The admin is read-only by design.
        return False

    def has_change_permission(self, request, obj=None):
        return False


class BlueprintWeightInline(admin.TabularInline):
    model = BlueprintWeight
    extra = 0
    fields = ('category', 'weight')
    autocomplete_fields = ['category']
    ordering = ('category_id',)


@admin.register(Blueprint)
class BlueprintAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'weight_count', 'created_by', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'description')
    inlines = [BlueprintWeightInline]

    def weight_count(self, obj):
        return obj.weight_entries.count()
    weight_count.short_description = 'Categories'


@admin.register(BlueprintWeight)
class BlueprintWeightAdmin(admin.ModelAdmin):
    list_display = ('blueprint', 'category', 'weight')
    list_filter = ('blueprint',)
    autocomplete_fields = ['blueprint', 'category']
