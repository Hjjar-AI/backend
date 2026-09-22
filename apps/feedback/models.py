# backend/apps/feedback/models.py
from django.db import models

from apps.core.models import TimeStampedModel


class Bookmark(TimeStampedModel):
    user = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='bookmarks',
    )
    question = models.ForeignKey(
        'questions.Question',
        on_delete=models.CASCADE,
        related_name='bookmarked_by',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'question'],
                name='bookmark_unique_per_user_question',
            ),
        ]


class QuestionFlag(TimeStampedModel):
    question = models.ForeignKey(
        'questions.Question',
        on_delete=models.CASCADE,
        related_name='flags',
    )
    user = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='question_flags',
    )
    reason = models.TextField(blank=True, null=True)
    resolved = models.BooleanField(default=False)
    resolved_by = models.CharField(max_length=80, blank=True, null=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    master_exam_attempt = models.ForeignKey(
        'master_exams.MasterExamAttempt',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='flags',
    )

    class Meta:
        constraints = [
            # One open flag per (question, user). A flag that has been
            # resolved does not count, so the same user can report a
            # second time on a question they previously reported and
            # which a moderator has since reopened.
            models.UniqueConstraint(
                fields=['question', 'user'],
                condition=models.Q(resolved=False),
                name='unique_open_flag_per_user_question',
            ),
        ]


class QuestionRating(TimeStampedModel):
    question = models.ForeignKey(
        'questions.Question',
        on_delete=models.CASCADE,
        related_name='ratings',
    )
    user = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='question_ratings',
    )
    rating = models.IntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['question', 'user'],
                name='qrating_unique_per_user_question',
            ),
            # The serializer enforces 1-5 at the API boundary. The
            # constraint below enforces it at the DB boundary, so a
            # script, migration, or Django-admin edit cannot store a
            # value outside the range and leave the aggregate
            # computation reading garbage.
            models.CheckConstraint(
                condition=models.Q(rating__gte=1) & models.Q(rating__lte=5),
                name='qrating_range_1_5',
            ),
        ]