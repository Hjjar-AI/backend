# backend/apps/learning/admin.py
from django.contrib import admin

from .models import UserQuestionAttempt


@admin.register(UserQuestionAttempt)
class UserQuestionAttemptAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'question', 'last_correct', 'last_confidence',
        'attempts', 'wrong_count', 'ever_correct',
        'interval_days', 'next_due',
    )
    list_filter = ('last_correct', 'ever_correct', 'last_confidence')
    search_fields = ('user__username', 'question__question')
    readonly_fields = ('last_answered_at',)

    def has_add_permission(self, request):
        return False