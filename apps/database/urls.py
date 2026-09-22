# backend/apps/database/urls.py

from django.urls import path
from . import views

urlpatterns = [
    path('info/', views.DatabaseInfoView.as_view(), name='db-info'),
    path('backup/', views.CreateBackupView.as_view(), name='db-backup'),
    path('backups/', views.ListBackupsView.as_view(), name='db-backups-list'),
    path('restore/', views.RestoreBackupView.as_view(), name='db-restore'),
    path('clear/', views.ClearDatabaseView.as_view(), name='db-clear'),
    path('import/', views.ImportDatabaseView.as_view(), name='db-import'),
    path('import/telegram/', views.ImportTelegramView.as_view(), name='db-import-telegram'),

    # ── Full questions-data envelope ─────────────────────────────
    # Placed before the wildcard `export/<str:fmt>/` route so the
    # literal `state` segment is not captured by the format matcher.
    path('export/state/', views.ExportStateView.as_view(), name='db-export-state'),
    path('import/state/', views.ImportStateView.as_view(), name='db-import-state'),

    path('export/<str:fmt>/', views.ExportDatabaseView.as_view(), name='db-export'),
    path('export/<str:fmt>/verified/', views.ExportVerifiedDatabaseView.as_view(), name='db-export-verified'),
]