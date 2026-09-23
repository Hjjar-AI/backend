# backend/apps/questions/urls.py

from django.urls import path

from . import views

urlpatterns = [
    path('', views.QuestionListView.as_view(), name='question-list'),
    path('batch/', views.QuestionBatchView.as_view(), name='question-batch'),
    path(
        'knowledge-objects/',
        views.KnowledgeObjectListCreateView.as_view(),
        name='knowledge-object-list',
    ),
    path(
        'knowledge-objects/<int:pk>/',
        views.KnowledgeObjectDetailView.as_view(),
        name='knowledge-object-detail',
    ),

    # ── Cases ─────────────────────────────────────────────────────────
    path('cases/', views.CaseListView.as_view(), name='case-list'),
    path('cases/<str:case_key>/', views.CaseDetailView.as_view(), name='case-detail'),
    # Kept under the singular prefix for backward compatibility with
    # the previous stem endpoint URL. New client code uses the plural
    # 'cases/' form above.
    path(
        'case/<str:case_key>/stem/',
        views.CaseStemUpdateView.as_view(),
        name='case-stem-update',
    ),

    path('tags/', views.TagListView.as_view(), name='tag-list'),

    path('bulk-verify/', views.BulkVerifyView.as_view(), name='bulk-verify'),
    path('bulk-tags/', views.BulkTagUpdateView.as_view(), name='bulk-tags'),
    path('unverified/', views.UnverifiedListView.as_view(), name='unverified-list'),
    path('available-count/', views.AvailableCountView.as_view(), name='available-count'),

    path('categories/', views.CategoryListView.as_view(), name='category-list'),
    path('categories/create/', views.CategoryCreateView.as_view(), name='category-create'),
    path(
        'categories/<int:category_id>/update/',
        views.CategoryUpdateView.as_view(),
        name='category-update',
    ),
    path(
        'categories/<int:category_id>/delete/',
        views.CategoryDeleteView.as_view(),
        name='category-delete',
    ),

    # ── Admin tag routes ──────────────────────────────────────────────
    # The former `admin/tags/` route (AdminTagListView) has been
    # deleted — it returned a byte-identical payload to `tags/` above.
    # The admin tag page now calls `/questions/tags/` directly. The
    # remaining `/questions/admin/tags/*` routes are distinct
    # operations (tree, rename, delete, merge) and stay.
    path('admin/tags/tree/', views.AdminTagTreeView.as_view(), name='admin-tags-tree'),
    path(
        'admin/tags/<path:old_name>/rename/',
        views.AdminTagRenameView.as_view(),
        name='admin-tag-rename',
    ),
    path(
        'admin/tags/<path:name>/delete/',
        views.AdminTagDeleteView.as_view(),
        name='admin-tag-delete',
    ),
    path('admin/tags/merge/', views.AdminTagMergeView.as_view(), name='admin-tags-merge'),

    # Per-question resource routes last. The int converter only matches
    # digits, so none of the literal paths above can be captured here.
    path('<int:question_id>/', views.QuestionDetailView.as_view(), name='question-detail'),
    path(
        '<int:question_id>/duplicate/',
        views.QuestionDuplicateView.as_view(),
        name='question-duplicate',
    ),
    path('<int:question_id>/verify/', views.ToggleVerifyView.as_view(), name='toggle-verify'),
    path(
        '<int:question_id>/image/',
        views.QuestionImageUploadView.as_view(),
        name='question-image',
    ),
]
