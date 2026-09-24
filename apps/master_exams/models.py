# backend/apps/master_exams/models.py

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.users.models import User


class MasterExam(TimeStampedModel):
    STORED_STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('published', 'Published'),
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed'),
        ('published_to_bank', 'Published to bank'),
    ]

    # ── Version-bump guard ──────────────────────────────────────────
    #
    # The previous save() bumped `version` on every save, including
    # status transitions like `draft → published`. That meant an
    # optimistic-lock conflict for an editor whose content had not
    # changed, because a status transition from another surface
    # (a second tab, a service call) advanced the version out from
    # under them. Content edits are the only thing an optimistic
    # lock is meant to guard against, so only content saves bump.
    #
    # M2M changes (audience_groups, audience_users, co_attendings)
    # are set AFTER save() via .set() and never ran through this
    # method, so their version impact is unchanged: still zero.
    #
    # ── LIFECYCLE STATE IS CONTENT (fix — concurrent-transition race) ─
    #
    # The four lifecycle fields — `stored_status`, `published_at`,
    # `completed_at`, `published_to_bank_at` — are members of
    # CONTENT_FIELDS. Adding them closes a race that the previous
    # revision left open: a status transition (`publish`, `cancel`,
    # `publish_to_bank`) mutated the exam row without touching
    # `version`, so a composition caller that had read the exam
    # before the transition still held a matching `version` and its
    # CAS write succeeded even though the exam's state had changed
    # out from under it.
    #
    # The transition itself is separately protected by a row lock in
    # `lifecycle.py` — this side of the fix is what propagates the
    # state change to optimistic-locking clients that are still
    # holding an old version.
    CONTENT_FIELDS = frozenset({
        'name', 'description', 'instructions', 'exam_topic_tag',
        'opens_at', 'closes_at', 'duration_minutes', 'allow_makeup',
        'shuffle_questions', 'shuffle_choices',
        'weight_easy', 'weight_medium', 'weight_hard',
        'audience_all_doctors',
        # Lifecycle status fields — see the block comment above.
        'stored_status', 'published_at', 'completed_at', 'published_to_bank_at',
    })

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    instructions = models.TextField(blank=True, null=True)

    primary_attending = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='primary_master_exams',
    )
    co_attendings = models.ManyToManyField(
        User,
        blank=True,
        related_name='co_master_exams',
    )

    opens_at = models.DateTimeField(db_index=True)
    closes_at = models.DateTimeField(db_index=True)
    duration_minutes = models.PositiveIntegerField()

    # The ordered question list is now a first-class relation
    # (MasterExamQuestion). The old `question_ids = JSONField(...)`
    # column is gone.

    audience_all_doctors = models.BooleanField(default=False)
    audience_groups = models.ManyToManyField(
        'groups.Group',
        blank=True,
        related_name='master_exams',
    )
    audience_users = models.ManyToManyField(
        User,
        blank=True,
        related_name='assigned_master_exams',
    )

    weight_easy = models.FloatField(default=1.0)
    weight_medium = models.FloatField(default=1.0)
    weight_hard = models.FloatField(default=1.0)

    shuffle_questions = models.BooleanField(default=True)
    shuffle_choices = models.BooleanField(default=False)
    allow_makeup = models.BooleanField(default=True)
    exam_topic_tag = models.CharField(max_length=100, blank=True, null=True)

    stored_status = models.CharField(
        max_length=24,
        choices=STORED_STATUS_CHOICES,
        default='draft',
    )
    version = models.IntegerField(default=1)

    published_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    published_to_bank_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-opens_at', '-created_at']
        indexes = [
            models.Index(fields=['stored_status', 'opens_at'], name='me_status_opens_idx'),
        ]

    def __str__(self):
        return self.name

    @property
    def computed_status(self):
        # Terminal stored statuses win unconditionally. 'completed' is
        # included here even though nothing currently writes it — a
        # legacy row, an admin edit, or a future migration that sets
        # it must not be reclassified back into 'scheduled'/'active'
        # merely because `opens_at` was later moved.
        #
        # Prior to this, `computed_status` only short-circuited on
        # 'cancelled' and 'published_to_bank'. A `stored_status ==
        # 'completed'` exam whose `opens_at` was subsequently edited
        # to a future date would report 'scheduled', which is a lie:
        # graded attempts already exist, and the UI tab grouping
        # (active / upcoming / completed) would move the exam into
        # the wrong bucket.
        if self.stored_status in ('cancelled', 'published_to_bank', 'completed'):
            return self.stored_status
        if self.stored_status == 'draft':
            return 'draft'
        now = timezone.now()
        if now < self.opens_at:
            return 'scheduled'
        if now < self.closes_at:
            return 'active'
        return 'completed'

    @property
    def is_published(self):
        return self.stored_status != 'draft'

    @property
    def can_edit_now(self):
        if self.stored_status in ('cancelled', 'published_to_bank'):
            return False
        return timezone.now() < self.opens_at

    @property
    def can_be_deleted(self):
        return self.stored_status != 'published_to_bank'

    # ── Ordered-question accessor shim ──────────────────────────────
    #
    # Read-only Python access to the ordered question-id list. Preserves
    # `exam.question_ids` for any code that only needs it in memory
    # (serializers, services, seeding). DB-level queryset filters on
    # `question_ids` (e.g. `values_list('question_ids', flat=True)`,
    # `filter(question_ids__contains=...)`) no longer work — those call
    # sites were migrated to `MasterExamQuestion` in this batch.
    @property
    def question_ids(self):
        return list(
            self.exam_questions
            .order_by('order')
            .values_list('question_id', flat=True)
        )

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        # A save with no explicit update_fields (i.e. "save everything")
        # bumps the version. A save with update_fields bumps only when
        # the touched fields intersect CONTENT_FIELDS.
        should_bump = (
            update_fields is None
            or bool(set(update_fields) & self.CONTENT_FIELDS)
        )
        if should_bump:
            self.version = (self.version or 0) + 1
            if update_fields is not None:
                kwargs['update_fields'] = list(set(update_fields) | {'version'})
        super().save(*args, **kwargs)


class MasterExamQuestion(models.Model):
    """
    Ordered membership of a Question inside a MasterExam.

    Replaces the previous `MasterExam.question_ids` JSONField, which
    stored the ordered list as opaque integers with no referential
    integrity. The FK on `question` uses PROTECT so a question that
    participates in any exam — live, completed, or archived — cannot
    be silently removed from under a results page.
    """
    master_exam = models.ForeignKey(
        MasterExam,
        on_delete=models.CASCADE,
        related_name='exam_questions',
    )
    question = models.ForeignKey(
        'questions.Question',
        on_delete=models.PROTECT,
        related_name='master_exam_usages',
    )
    order = models.PositiveIntegerField()

    class Meta:
        unique_together = ('master_exam', 'question')
        ordering = ['order']
        indexes = [
            models.Index(fields=['master_exam', 'order'], name='meq_exam_order_idx'),
        ]

    def __str__(self):
        return f'{self.master_exam_id} · Q{self.question_id} @ {self.order}'


class MasterExamAttempt(TimeStampedModel):
    master_exam = models.ForeignKey(
        MasterExam,
        on_delete=models.CASCADE,
        related_name='attempts',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='master_exam_attempts',
    )
    session_id = models.CharField(max_length=36, unique=True)

    # The attempt keeps its own frozen snapshot of the question list.
    # This is intentional and is unrelated to the exam-side list: an
    # author editing the exam's questions during the window must NOT
    # change the questions of an in-flight attempt.
    question_ids = models.JSONField(default=list)
    grading_snapshot = models.JSONField(default=dict)

    answers = models.JSONField(default=dict)
    current_question_id = models.IntegerField(null=True, blank=True)
    started_at = models.DateTimeField()
    deadline_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    results = models.JSONField(null=True, blank=True)
    correct_count = models.IntegerField(default=0)
    total_questions = models.IntegerField(default=0)
    answered_count = models.IntegerField(default=0)
    weighted_score = models.FloatField(default=0.0)
    accuracy = models.FloatField(default=0.0)
    is_complete = models.BooleanField(default=False, db_index=True)
    is_makeup = models.BooleanField(default=False)
    forced_finish = models.BooleanField(default=False)
    exam_name_snapshot = models.CharField(max_length=200)
    class Meta:
        ordering = ['-started_at']
        unique_together = ('master_exam', 'user', 'is_makeup')
        indexes = [
            models.Index(fields=['master_exam', 'is_complete'], name='mea_exam_complete_idx'),
            models.Index(fields=['user', '-started_at'], name='mea_user_started_idx'),
        ]

    def __str__(self):
        state = 'done' if self.is_complete else 'active'
        return f"{self.user.username} @ {self.master_exam.name} ({state})"

    @property
    def is_expired(self):

        grace_seconds = getattr(settings, 'MASTER_EXAM_GRACE_SECONDS', 180)
        return timezone.now() > self.deadline_at + timedelta(seconds=grace_seconds)


class MasterExamAcknowledgement(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='master_exam_acknowledgements',
    )
    master_exam = models.ForeignKey(
        MasterExam,
        on_delete=models.CASCADE,
        related_name='acknowledgements',
    )
    acknowledged_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('user', 'master_exam')
        indexes = [
            models.Index(fields=['user', 'master_exam'], name='mea_ack_user_exam_idx'),
        ]

    def __str__(self):
        return f"{self.user.username} acknowledged {self.master_exam.name}"
