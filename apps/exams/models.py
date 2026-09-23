# backend/apps/exams/models.py
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.users.models import User


class ExamSession(models.Model):
    """
    Live session state for a plain (non-master-exam) run.

    A row exists for the duration of a session and is deleted on
    finish. This is the "hot" table: it is written on every answer
    and read on every page turn.

    HISTORY (removed shadow row)
    ----------------------------
    Prior to this revision, a `MasterExamAttempt` also created a row
    here (linked via a `master_exam_attempt` OneToOneField) so the
    daily `cleanup_database` sweep would prune abandoned sessions on
    the same schedule as plain ones. That shadow row was written on
    every master exam start, mirrored on every answer via
    `MasterExamAttemptService._sync_session`, and deleted on finish.

    Nothing ever *read* it. The master exam status, current question,
    submit-answer, and finish endpoints all read `MasterExamAttempt`
    directly. The `cleanup_database` sweep now calls
    `MasterExamSweeper.sweep_expired()` on its own — the shadow was
    dead infrastructure that existed only to keep two rows in sync
    that were never allowed to diverge.

    The `master_exam_attempt` FK has been removed from this model.
    Any orphaned shadow rows in an existing database age out through
    the daily sweep on the same 7-day cutoff they always did.

    GRADING SNAPSHOT (fix — issue: mutable grading inputs)
    ------------------------------------------------------
    Prior to this revision, `finish_session` graded against the live
    `Question` rows: `grade_exam(locked.question_ids, locked.answers)`
    reloaded each question by id at grade time. Editing a question's
    `correct_answer` or `choices` mid-session changed the outcome of
    an in-progress exam, and deleting a question silently dropped it
    from `present_count` (the accuracy denominator) because
    `grade_exam` skipped `None` rows.

    `grading_snapshot` is a per-session, JSON-keyed freeze of every
    question's content and answer at the moment the session started.
    `ExamService.grade_exam` reads it first and only falls back to
    the live row when a snapshot entry is absent (sessions created
    before this column existed). Deleting a question no longer
    affects an in-progress session — the snapshot is self-sufficient.

    The shape mirrors `MasterExamAttempt.grading_snapshot` so both
    exam types store the same fields under the same keys.
    """
    MODE_CHOICES = [
        ('exam', 'Exam'),
        ('study', 'Study'),
        ('recall', 'Answer before options'),
    ]

    session_id = models.CharField(max_length=36, unique=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='exam_sessions')
    mode = models.CharField(max_length=20, choices=MODE_CHOICES)
    question_ids = models.JSONField(default=list)
    answers = models.JSONField(default=dict)
    current_index = models.IntegerField(default=0)
    tag = models.CharField(max_length=100, blank=True, null=True)
    started_at = models.DateTimeField(default=timezone.now)
    accumulated_time = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    blueprint = models.ForeignKey(
        'Blueprint',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sessions',
    )

    # Frozen grading inputs for this session. Populated once at
    # session start and never written again. See the class docstring.
    grading_snapshot = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.user.username} - {self.mode} - {self.session_id}"


class TestHistory(models.Model):
    """
    Immutable record of one completed session, keyed by mode.

    CONSOLIDATION (previously `StudySession` was a near-duplicate)
    --------------------------------------------------------------
    `StudySession` was written in the same transaction as every
    TestHistory row, carried the same (user, tag, total_questions,
    correct_count, accuracy, time_spent, completed_at) payload, and
    read by nothing that TestHistory did not already serve. It has
    been removed. The two things it uniquely carried — `started_at`,
    and an FK with CASCADE delete semantics — are now handled by
    this model:

      • `started_at` — a new nullable column. Legacy rows (created
        before the consolidation) have NULL; new rows always carry a
        value. The serializer does not expose it; only in-process
        callers read it.

      • `user` — stays SET_NULL (as it always was on TestHistory).
        The prior `StudySession` row would have been CASCADE-deleted
        with its user; after consolidation, the history row survives
        the user's deletion and renders as "محذوف" (deleted) via the
        serializer's `default=` fallback. This is the more correct
        behaviour for a history table and matches what TestHistory
        already did.

    INDEX STRATEGY
    --------------
    Every reader (analytics trend, active-users chart, group
    leaderboard, planner progress, activity heatmap, admin list
    subqueries) filters on `user_id` plus a `completed_at` range
    window, ordered descending. The two indexes below cover both the
    user-scoped and the global time-window variants.
    """
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='test_history',
    )
    mode = models.CharField(max_length=20)
    tag = models.CharField(max_length=100, blank=True, null=True)
    total_questions = models.IntegerField()
    correct_count = models.IntegerField()
    accuracy = models.FloatField()
    time_spent = models.IntegerField()
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-completed_at']
        indexes = [
            models.Index(fields=['user', '-completed_at'], name='th_user_completed_idx'),
            models.Index(fields=['-completed_at'], name='th_completed_idx'),
        ]

    def __str__(self):
        return f"{self.user.username if self.user else 'Deleted'} - {self.mode} - {self.completed_at}"


class Blueprint(TimeStampedModel):
    """
    A per-category question-distribution template for exam assembly.

    The category weights used to live in a `weights` JSONField keyed on
    category IDs as strings. That made category deletion a silent
    corruption path (dangling integer keys, filtered out at selection
    time) and forced the admin UI to render opaque IDs.

    The weights are now a first-class relation (BlueprintWeight) with
    a real FK to Category and a real FloatField weight. Deleting a
    category cascades its weight row away, which is the correct
    semantic.

    The `weights` property supplies the legacy dict shape to both the
    serializer and in-process callers.
    """
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_by = models.CharField(max_length=80, blank=True, null=True)
    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def weights(self):
        """
        Read-only dict view of the through table, keyed on category id
        as a string. Preserves the shape that predates the model
        change, so any in-process caller that only reads `bp.weights`
        keeps working. Write paths go through BlueprintWeight directly.
        """
        return {
            str(entry.category_id): entry.weight
            for entry in self.weight_entries.all()
        }


class BlueprintWeight(models.Model):
    """
    Per-category weight for a Blueprint.

    A weight of 0 is not stored — the absent row is the representation
    of "category not in this blueprint". Weights are relative, not
    percentages; the selection algorithm divides by the sum.
    """
    blueprint = models.ForeignKey(
        Blueprint,
        on_delete=models.CASCADE,
        related_name='weight_entries',
    )
    category = models.ForeignKey(
        'questions.Category',
        on_delete=models.CASCADE,
        related_name='blueprint_weights',
    )
    weight = models.FloatField()

    class Meta:
        unique_together = ('blueprint', 'category')
        ordering = ['category_id']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(weight__gt=0),
                name='bw_weight_positive',
            ),
        ]

    def __str__(self):
        return f'{self.blueprint_id} · cat={self.category_id} = {self.weight}'
