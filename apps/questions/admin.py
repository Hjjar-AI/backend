# backend/apps/questions/admin.py

from django.contrib import admin
from django.utils import timezone

from .models import (
    Category,
    Tag,
    Question,
    ClinicalCase,
    ExternalAuthorMapping,
    KnowledgeObject,
)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'uuid', 'color', 'icon', 'created_by', 'created_at')
    search_fields = ('name',)
    readonly_fields = ('uuid', 'created_at', 'updated_at')


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ('name', 'uuid', 'parent')
    search_fields = ('name',)
    readonly_fields = ('uuid',)


class CaseQuestionInline(admin.TabularInline):
    model = Question
    extra = 0
    fields = ('id', 'case_order', 'question', 'difficulty', 'verified')
    readonly_fields = ('id', 'question', 'difficulty', 'verified')
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ClinicalCase)
class ClinicalCaseAdmin(admin.ModelAdmin):
    list_display = (
        'key', 'uuid', 'title', 'question_count', 'authored_by', 'updated_at',
    )
    search_fields = ('key', 'title', 'stem')
    readonly_fields = ('uuid', 'created_at', 'updated_at')
    inlines = [CaseQuestionInline]
    fieldsets = (
        (None, {'fields': ('key', 'title', 'stem')}),
        ('Ownership', {'fields': ('authored_by',)}),
        ('Timestamps', {'fields': ('uuid', 'created_at', 'updated_at')}),
    )

    def question_count(self, obj):
        return obj.questions.count()
    question_count.short_description = 'Questions'


@admin.register(KnowledgeObject)
class KnowledgeObjectAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'status', 'category', 'question_count',
        'last_revised_at', 'created_by',
    )
    list_filter = ('status', 'category')
    search_fields = ('title', 'learning_objective', 'canonical_answer')
    readonly_fields = ('uuid', 'created_at', 'updated_at', 'version')
    filter_horizontal = ('tags',)

    def question_count(self, obj):
        return obj.questions.count()


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'question_preview', 'difficulty', 'category',
        'authored_by', 'owned_by', 'verified',
        'times_answered', 'times_correct',
    )
    list_filter = ('difficulty', 'verified', 'category')
    search_fields = ('question', 'explanation', 'source', 'source_document')
    actions = ['bulk_verify', 'bulk_unverify']
    list_select_related = ('authored_by', 'owned_by', 'category')
    fields = (
        'question', 'image', 'choices', 'correct_answer', 'explanation',
        'source', 'source_document', 'source_page', 'translations',
        'difficulty', 'category', 'knowledge_object', 'last_revised_at',
        'verified', 'verified_by',
        'verified_at', 'verification_notes',
        # Authorship and ownership — two FKs, two independent facts.
        # `authored_by` is the writer; `owned_by` is the current
        # steward. Both must be non-null on a published question
        # (CheckConstraint on the model); a draft may temporarily
        # have owned_by=NULL during creation.
        'authored_by', 'owned_by',
        'updated_by', 'times_answered', 'times_correct', 'version',
        # Case linkage.
        'case', 'case_order',
    )

    def question_preview(self, obj):
        return obj.question[:100]
    question_preview.short_description = 'Question'

    # ── Author reputation recompute on admin verify/unverify ────────
    #
    # REPUTATION RECOMPUTE (fix — issue: stored counters drift)
    # ---------------------------------------------------------
    # `AuthorReputationService.refresh_users` derives
    # `questions_count` and `trust_score` from the set of PUBLIC
    # questions where `authored_by` is the user, counting verified
    # vs. total. Both admin actions below change the `verified` flag
    # on a set of questions, which changes every affected author's
    # `trust_score` — but until this revision, they called
    # `queryset.update(...)` and never touched the User table, so the
    # stored `trust_score` kept reporting the pre-verify state until
    # someone ran `manage.py refresh_author_ranks` manually.
    #
    # Both actions now collect the affected author ids and hand them
    # to `QuestionService._recompute_author_trust`, which collapses
    # the whole recompute into one aggregate query plus one bulk
    # update regardless of how many authors are touched.
    #
    # The recompute runs AFTER the queryset.update() so the aggregate
    # reads the new `verified` values. It is not wrapped in a
    # transaction with the update — the two are independent and a
    # failure in the recompute leaves the counter stale rather than
    # the verification wrong, which is the recoverable direction.

    def _recompute_affected_authors(self, question_ids):
        from .services import QuestionService
        author_ids = list(
            Question.objects
            .filter(id__in=question_ids, authored_by_id__isnull=False)
            .values_list('authored_by_id', flat=True)
            .distinct()
        )
        if author_ids:
            QuestionService._recompute_author_trust(author_ids)

    def bulk_verify(self, request, queryset):
        ids = list(queryset.values_list('id', flat=True))
        queryset.update(
            verified=True,
            verified_by=request.user.username,
            verified_at=timezone.now(),
        )
        self._recompute_affected_authors(ids)
        self.message_user(request, f'{queryset.count()} questions verified.')
    bulk_verify.short_description = 'Verify selected questions'

    def bulk_unverify(self, request, queryset):
        ids = list(queryset.values_list('id', flat=True))
        # `verification_notes` is cleared on unverify, matching the
        # service-layer path in `QuestionService.bulk_unverify`. The
        # previous admin action left the notes in place, so the same
        # logical operation produced two different final states
        # depending on which surface it ran on.
        queryset.update(
            verified=False,
            verified_by=None,
            verified_at=None,
            verification_notes=None,
        )
        self._recompute_affected_authors(ids)
        self.message_user(request, f'{queryset.count()} questions unverified.')
    bulk_unverify.short_description = 'Unverify selected questions'


@admin.register(ExternalAuthorMapping)
class ExternalAuthorMappingAdmin(admin.ModelAdmin):
    """
    Fallback editor for the per-source-author decisions made during
    state envelope imports.

    The import modal writes these rows automatically. This admin page
    exists so a wrong decision can be corrected after the fact — the
    next import of the same source applies the corrected value.
    """
    list_display = (
        'source_username', 'action', 'target_user', 'decided_by', 'created_at',
    )
    list_filter = ('action',)
    search_fields = ('source_username', 'target_user__username')
    readonly_fields = ('created_at', 'updated_at', 'decided_by')
    ordering = ('source_username',)
    fieldsets = (
        (None, {
            'fields': ('source_username', 'action', 'target_user'),
            'description': (
                'action="user" requires target_user; the other actions '
                'must leave it empty. Enforced by a CheckConstraint.'
            ),
        }),
        ('Audit', {
            'fields': ('decided_by', 'created_at', 'updated_at'),
        }),
    )
