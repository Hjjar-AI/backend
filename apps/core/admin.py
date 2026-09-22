from django.contrib import admin
from .models import Setting, PrivilegedAction, Tip


@admin.register(Setting)
class SettingAdmin(admin.ModelAdmin):
    list_display = ('key', 'value', 'updated_at')

@admin.register(PrivilegedAction)
class PrivilegedActionAdmin(admin.ModelAdmin):
    list_display = (
        'timestamp',
        'actor_username',
        'action',
        'target_type',
        'target_id',
        'target_repr',
        'ip',
    )
    list_filter = ('action', 'target_type')
    search_fields = ('actor_username', 'action', 'target_repr', 'ip')
    date_hierarchy = 'timestamp'
    ordering = ('-timestamp',)
    readonly_fields = (
        'actor',
        'actor_username',
        'action',
        'target_type',
        'target_id',
        'target_repr',
        'ip',
        'user_agent',
        'details',
        'timestamp',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_staff


@admin.register(Tip)
class TipAdmin(admin.ModelAdmin):
    list_display = ('text_preview', 'is_active', 'order', 'updated_at')
    list_editable = ('is_active', 'order')
    list_filter = ('is_active',)
    search_fields = ('text',)
    ordering = ('order', 'id')
    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        (None, {'fields': ('text',)}),
        ('Display', {'fields': ('is_active', 'order')}),
        ('Timestamps', {'fields': ('created_at', 'updated_at')}),
    )

    def text_preview(self, obj):
        return obj.text[:80]
    text_preview.short_description = 'Tip'