# backend/apps/planning/models.py

from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.users.models import User


class StudyPlanner(TimeStampedModel):
    """
    A per-user study target.

    The two target lists used to be JSON arrays: `target_categories` a
    list of category ids, `target_tags` a list of tag-name strings.
    Both had the same dangling-reference problem as Blueprint.weights:
    deleting a category left a stale integer in every planner that
    referenced it, and nothing enforced that a stored tag name still
    corresponded to a live Tag row. The lists are now M2M relations.

    The wire format is unchanged: the serializer emits
    `target_categories` as a list of category ids and `target_tags` as
    a list of tag names (not tag ids), because the frontend already
    reads and writes them that way.

    `daily_progress` also used to be a JSON dict keyed on date. It is
    now a StudyPlannerDay relation, one row per study day. The
    serializer still emits the same `{date_str: count}` dict shape so
    the frontend does not need to change.
    """
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='study_planner',
    )
    target_questions_per_day = models.IntegerField(
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(1000)],
    )
    target_categories = models.ManyToManyField(
        'questions.Category',
        blank=True,
        related_name='planner_subscriptions',
    )
    target_tags = models.ManyToManyField(
        'questions.Tag',
        blank=True,
        related_name='planner_subscriptions',
    )
    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)
    def __str__(self):
        return f"Planner: {self.user.username}"


class StudyPlannerDay(models.Model):
    """
    A single day's study progress for a planner.

    Replaces the `daily_progress` JSONField. The old representation
    rewrote the entire historical blob on every study day, and had no
    bound — a three-year-old account carried a JSON dict with a
    thousand keys on one row. This is a bounded single-row upsert per
    study day, indexed for the weekly-view query.
    """
    planner = models.ForeignKey(
        StudyPlanner,
        on_delete=models.CASCADE,
        related_name='days',
    )
    date = models.DateField()
    questions_answered = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('planner', 'date')
        ordering = ['-date']
        indexes = [
            models.Index(fields=['planner', '-date'], name='spd_planner_date_idx'),
        ]

    def __str__(self):
        return (
            f'{self.planner.user.username} · {self.date} · '
            f'{self.questions_answered}'
        )
