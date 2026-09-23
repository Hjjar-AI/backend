# backend/apps/questions/models.py

import uuid

from django.db import models

from apps.users.models import User
from apps.core.models import TimeStampedModel

TAG_NAME_MAX_LENGTH = 50
CATEGORY_NAME_MAX_LENGTH = 100
QUESTION_TEXT_MAX_LENGTH = 3000
EXPLANATION_TEXT_MAX_LENGTH = 3000
CHOICE_TEXT_MAX_LENGTH = 300
CASE_STEM_MAX_LENGTH = 3000
CASE_GROUP_MAX_LENGTH = 64
SOURCE_DOCUMENT_MAX_LENGTH = 500


def clean_tag_name(name):
    return name.strip()[:TAG_NAME_MAX_LENGTH]


class QuestionQuerySet(models.QuerySet):
    def public(self):
        return self.filter(is_draft=False)

    def drafts_for(self, user):
        if user is None or not getattr(user, 'is_authenticated', False):
            return self.none()
        return self.filter(is_draft=True, draft_owner=user)

    def visible_to(self, user):
        if user is None or not getattr(user, 'is_authenticated', False):
            return self.public()
        return self.filter(models.Q(is_draft=False) | models.Q(draft_owner=user))


class QuestionManager(models.Manager.from_queryset(QuestionQuerySet)):
    pass


class Category(TimeStampedModel):
    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
    )
    name = models.CharField(max_length=CATEGORY_NAME_MAX_LENGTH, unique=True)
    description = models.TextField(blank=True, null=True)
    color = models.CharField(max_length=20, default='#667eea')
    icon = models.CharField(max_length=50, default='bi-folder')

    # NOTE: `created_by` remains a plain string. Categories are a
    # lightweight taxonomy; their creator is display-only, never
    # consulted for permission or reputation. Converting it to an FK
    # would buy nothing the two-FK split on Question already provides.
    created_by = models.CharField(max_length=80, blank=True, null=True)

    def __str__(self):
        return self.name


class Tag(models.Model):
    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
    )
    name = models.CharField(max_length=TAG_NAME_MAX_LENGTH, unique=True)
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='children',
    )

    def __str__(self):
        return self.name


class ClinicalCase(TimeStampedModel):
    """
    A clinical vignette shared by a set of questions.

    Replaces the previous denormalized `Question.case_group` (a
    CharField key) plus `Question.case_stem` (a TextField duplicated
    across every sibling). The `key` string is preserved verbatim from
    the old `case_group` values so existing URLs, seed data, and
    export files continue to resolve.

    Deleting a case does NOT delete its questions — the FK on
    Question uses SET_NULL. The questions become standalone.

    The `authored_by` field is display-only provenance. It does NOT
    drive permissions or reputation — see the two-FK design on
    Question for where those live.
    """
    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
    )
    key = models.CharField(
        max_length=CASE_GROUP_MAX_LENGTH,
        unique=True,
        db_index=True,
        help_text=(
            'Human-readable identifier, e.g. "case-depression-01". '
            'Preserved from the previous Question.case_group values.'
        ),
    )
    title = models.CharField(
        max_length=200,
        blank=True,
        null=True,
        help_text='Optional title shown in admin lists.',
    )
    stem = models.TextField(
        max_length=CASE_STEM_MAX_LENGTH,
        blank=True,
        null=True,
        help_text='Shared clinical vignette rendered above every question in the case.',
    )
    authored_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='authored_cases',
        help_text=(
            'Who created this case. Historical provenance only — '
            'SET_NULL so a user can be removed without losing the '
            'case itself. Not used for permission or reputation.'
        ),
    )

    class Meta:
        ordering = ['key']
        indexes = [
            models.Index(fields=['key'], name='cc_key_idx'),
        ]

    def __str__(self):
        return self.title or self.key


class Question(TimeStampedModel):
    DIFFICULTY_CHOICES = [
        ('easy', 'Easy'),
        ('medium', 'Medium'),
        ('hard', 'Hard'),
    ]

    # ── Portable identity ─────────────────────────────────────────────
    #
    # Stable identifier used by the state envelope. Re-importing an
    # envelope resolves questions by uuid instead of by question text,
    # so the operation is idempotent and a reworded question is not
    # duplicated.
    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
    )

    question = models.TextField(max_length=QUESTION_TEXT_MAX_LENGTH)
    choices = models.JSONField(default=list)
    correct_answer = models.IntegerField()
    explanation = models.TextField(
        max_length=EXPLANATION_TEXT_MAX_LENGTH,
        blank=True,
        null=True,
    )
    source = models.CharField(max_length=200, blank=True, null=True)
    source_document = models.CharField(
        max_length=SOURCE_DOCUMENT_MAX_LENGTH,
        blank=True,
        null=True,
        help_text='Original document, file, book, or URL this question came from.',
    )
    source_page = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text='One-based page number inside source_document.',
    )
    translations = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            'Localized content keyed by locale, e.g. '
            '{"en": {"question": "...", "choices": [...], '
            '"explanation": "..."}}.'
        ),
    )
    difficulty = models.CharField(max_length=20, choices=DIFFICULTY_CHOICES, default='medium')
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='questions',
    )
    verified = models.BooleanField(default=False)
    verified_by = models.CharField(max_length=80, blank=True, null=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_notes = models.TextField(blank=True, null=True)

    # ═══════════════════════════════════════════════════════════════════
    # AUTHORSHIP AND OWNERSHIP — the two-FK split
    # ═══════════════════════════════════════════════════════════════════
    #
    # `authored_by` and `owned_by` are two independent facts about a
    # question. They are set to the same user at creation, and only
    # diverge when ownership is explicitly handed off.
    #
    # WHY TWO FIELDS
    # --------------
    # A single "author" field has to answer two questions that do not
    # always have the same answer:
    #
    #   • Who wrote this?          → `authored_by`
    #   • Who maintains this now?  → `owned_by`
    #
    # When Dr. Sarah leaves and Dr. Khalil takes over her questions,
    # those two questions have different answers. Collapsing them into
    # one field forces a choice: either Khalil is falsely credited as
    # the author, or Sarah remains the owner of questions she no
    # longer maintains. Neither is correct.
    #
    # WHAT READS WHICH
    # ----------------
    #   • `_is_own_question` (edit/delete gate)  → owned_by
    #   • `AuthorReputationService.refresh_user` → authored_by
    #   • Profile "your questions" count         → authored_by
    #   • "My questions" admin filter            → owned_by
    #   • Question card display                  → both
    #   • Import: setting both                   → see import_service
    #
    # DELETION
    # --------
    # Both use PROTECT. Deleting a user who authored or owns any
    # question is refused at the DB level. The intended workflow is
    # `User.deactivate()` for the everyday case; a hard delete only
    # happens after ownership has been explicitly reassigned.
    #
    # NULLS
    # -----
    # `authored_by` is nullable for two cases:
    #   1. Seed content. `seed_sample_questions` runs before any real
    #      author exists; those rows have no human author.
    #   2. Imports whose source author could not be resolved. The
    #      import flow offers three options per unknown name —
    #      assign to a local user, create a stub, or leave NULL —
    #      and only the last produces a null here.
    #
    # `owned_by` is nullable only for drafts. A published question
    # must have an owner; a CheckConstraint below enforces it. In
    # practice every published question gets `owned_by` set at
    # creation and never cleared.
    authored_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='authored_questions',
        help_text=(
            'Who wrote this question. Set at creation, never '
            'reassigned. NULL for seed content and for imports whose '
            'author could not be resolved.'
        ),
    )
    owned_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='owned_questions',
        help_text=(
            'Who is responsible for maintaining this question now. '
            'Drives the edit/delete gate for members who hold only '
            'the _own capability. Reassignable by an admin.'
        ),
    )

    # Kept for audit. Not authoritative for permission or reputation.
    updated_by = models.CharField(max_length=80, blank=True, null=True)

    times_answered = models.IntegerField(default=0)
    times_correct = models.IntegerField(default=0)
    version = models.IntegerField(default=1)
    tags = models.ManyToManyField(Tag, through='QuestionTag')

    image = models.FileField(
        upload_to='question_images/%Y/%m/',
        null=True,
        blank=True,
    )

    is_draft = models.BooleanField(default=False, db_index=True)
    draft_owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='draft_questions',
        help_text='Set when is_draft=True. The attending who owns this draft.',
    )

    # ── Case linkage (replaces case_group + case_stem) ─────────────
    case = models.ForeignKey(
        ClinicalCase,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='questions',
        db_index=True,
    )
    case_order = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            'Position within the case when siblings are shuffled. '
            'NULL means "use insertion order".'
        ),
    )

    objects = QuestionManager()

    class Meta:
        constraints = [
            # A published question must have an owner. Drafts may
            # briefly have owned_by=NULL during creation.
            models.CheckConstraint(
                condition=models.Q(is_draft=True) | models.Q(owned_by__isnull=False),
                name='question_published_has_owner',
            ),
        ]

    def __str__(self):
        return self.question[:50]

    def save(self, *args, **kwargs):
        # Range check on correct_answer. `choices` is a JSONField list,
        # so the constraint cannot be enforced at the DB layer. This
        # guard runs on every .save() call (admin, seed, scripts) but
        # not on .update() or .bulk_create() — see the docstring note
        # in QuestionService for the paths that bypass it.
        #
        # SINGLE SOURCE OF TRUTH (fix — validation.py owns the message)
        # -------------------------------------------------------------
        # The previous version of this guard hardcoded an English
        # message ("correct_answer {n} out of range for {m} choices")
        # here. That made it the fourth copy of the correct-answer
        # range rule — after validation.clean_and_validate_choices,
        # flat_import._build_question, and
        # QuestionUpdateSerializer.validate — and the only copy in
        # English rather than Arabic. A caller that reached this
        # branch (typically via a management command or a reverse-
        # parse restore calling .save() directly) got a differently-
        # shaped error than every other entry point produced for the
        # identical business rule.
        #
        # The guard now delegates to `validate_correct_answer`, which
        # owns both the rule and the message. The import is done
        # lazily inside `save()` because `validation.py` imports
        # `CHOICE_TEXT_MAX_LENGTH` from this module at load time —
        # a module-level import here would deadlock the app registry.
        choices = self.choices if isinstance(self.choices, list) else []
        if choices:
            from .validation import validate_correct_answer
            error = validate_correct_answer(
                int(self.correct_answer or 0),
                len(choices),
            )
            if error is not None:
                raise ValueError(error['message'])
        super().save(*args, **kwargs)


class QuestionTag(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('question', 'tag')


class ExternalAuthorMapping(TimeStampedModel):
    """
    Records an admin's decision about how to attribute questions
    whose author name appears in an import envelope but does not match
    any local User.

    CONSULTED AUTOMATICALLY
    -----------------------
    On every subsequent import, before flagging an author name as
    unknown, the import flow consults this table. If a decision exists,
    it is applied silently — the admin is not prompted again. This
    means each external author is decided once, ever, and the
    decision persists across imports.

    EDITABLE FROM THE ADMIN
    -----------------------
    A wrong decision can be corrected from Django admin. The next
    import applies the new value.

    TIMESTAMP
    ---------
    `created_at` (inherited from TimeStampedModel) IS the decision
    timestamp. There is no separate `decided_at` field — the row is
    created exactly once per source_username, at decision time.
    `updated_at` tracks later edits to the decision.

    ACTIONS
    -------
    user  → attribute to an existing local User (target_user must be set)
    stub  → create (or reuse) a stub User with the same username
    null  → leave `authored_by = NULL`; the question is unattributed
    """
    ACTION_USER = 'user'
    ACTION_STUB = 'stub'
    ACTION_NULL = 'null'

    ACTION_CHOICES = [
        (ACTION_USER, 'Assign to existing user'),
        (ACTION_STUB, 'Create stub user'),
        (ACTION_NULL, 'Leave as unknown'),
    ]

    source_username = models.CharField(
        max_length=80,
        unique=True,
        db_index=True,
        help_text='The author name as it appears in the import envelope.',
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    target_user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='external_author_mappings',
        help_text='Required when action is "user"; ignored otherwise.',
    )
    decided_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='author_mapping_decisions',
        help_text='Audit only. NULL if the deciding admin is later removed.',
    )

    class Meta:
        ordering = ['source_username']
        verbose_name = 'External author mapping'
        verbose_name_plural = 'External author mappings'
        constraints = [
            # action='user' requires a target; other actions must not
            # have one (a stray target on a 'stub' or 'null' row would
            # be misleading).
            models.CheckConstraint(
                condition=(
                    (models.Q(action='user') & models.Q(target_user__isnull=False)) |
                    (~models.Q(action='user') & models.Q(target_user__isnull=True))
                ),
                name='external_author_mapping_action_matches_target',
            ),
        ]

    def __str__(self):
        if self.action == self.ACTION_USER and self.target_user_id:
            return f'{self.source_username} → {self.target_user.username}'
        return f'{self.source_username} → {self.action}'
