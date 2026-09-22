# backend/apps/feedback/admin.py

from django.contrib import admin
from django.utils import timezone

from .models import Bookmark, QuestionFlag, QuestionRating


@admin.register(Bookmark)
class BookmarkAdmin(admin.ModelAdmin):
    list_display = ('user', 'question', 'created_at')


@admin.register(QuestionFlag)
class QuestionFlagAdmin(admin.ModelAdmin):
    list_display = ('question', 'user', 'reason', 'resolved', 'created_at')
    list_filter = ('resolved',)
    actions = ['resolve_flags']

    def resolve_flags(self, request, queryset):
        queryset.update(
            resolved=True,
            resolved_by=request.user.username,
            resolved_at=timezone.now(),
        )
        self.message_user(request, f'{queryset.count()} flags resolved.')
    resolve_flags.short_description = 'Resolve selected flags'


@admin.register(QuestionRating)
class QuestionRatingAdmin(admin.ModelAdmin):
    list_display = ('question', 'user', 'rating', 'created_at')