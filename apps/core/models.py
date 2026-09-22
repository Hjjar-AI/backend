# backend/apps/core/models.py

from django.db import models
from django.utils import timezone

LOCALE_AR = 'ar'
LOCALE_EN = 'en'

LOCALE_CHOICES = [
    (LOCALE_AR, 'Arabic'),
    (LOCALE_EN, 'English'),
]

SUPPORTED_LOCALES = {LOCALE_AR, LOCALE_EN}
DEFAULT_LOCALE = LOCALE_AR


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Setting(models.Model):
    key = models.CharField(max_length=50, primary_key=True)
    value = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key


class PrivilegedAction(models.Model):
    """
    Append-only audit log for administrative operations.

    RETENTION POLICY
    ----------------
    Rows are retained for PRIVILEGED_ACTION_RETENTION_DAYS (default
    365, configurable in settings.py). The sweep runs as part of
    `cleanup_database`, and is exposed as a standalone command
    (`manage.py cleanup_privileged_actions`) for cron use.

    This is the ONLY table in the codebase that is append-only by
    design. Every other state table is either mutable or cascades on
    delete. Two consequences worth knowing:

      • The `timestamp` index is load-bearing — the retention sweep
        is a range scan on it. Removing the index turns the sweep
        into a table scan.

      • Deleting a PrivilegedAction row is a deliberate act. Do not
        add a `has_delete_permission = True` Django-admin hook that
        would let a moderator prune individual rows, and do not add
        a cascade from `User` — the row is meant to survive the
        actor's deletion, which is why `actor` uses SET_NULL and
        `actor_username` carries a denormalized copy.

    Retention is bounded by age, not by count. A quiet year leaves
    fewer rows than a busy month, and that is the correct behavior —
    the audit trail for a specific incident must not be evicted
    because the site got busy later.
    """
    actor = models.ForeignKey(
        'users.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='privileged_actions',
    )
    actor_username = models.CharField(max_length=80, blank=True, null=True)
    action = models.CharField(max_length=80)
    target_type = models.CharField(max_length=80, blank=True, null=True)
    target_id = models.CharField(max_length=80, blank=True, null=True)
    target_repr = models.CharField(max_length=255, blank=True, null=True)
    ip = models.CharField(max_length=45, blank=True, null=True)
    user_agent = models.CharField(max_length=255, blank=True, null=True)
    details = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['actor', '-timestamp'], name='core_pa_actor_idx'),
            models.Index(fields=['action', '-timestamp'], name='core_pa_action_idx'),
        ]

    def __str__(self):
        who = self.actor_username or '?'
        return f"{who} · {self.action} · {self.timestamp:%Y-%m-%d %H:%M:%S}"


class Tip(TimeStampedModel):

    text = models.TextField(max_length=500)
    locale = models.CharField(
        max_length=2,
        choices=LOCALE_CHOICES,
        default=DEFAULT_LOCALE,
        db_index=True,
        help_text='The language this tip is written in.',
    )
    is_active = models.BooleanField(default=True)
    order = models.IntegerField(default=0)
    class Meta:

        ordering = ['order', 'id']
        indexes = [
            models.Index(
                fields=['locale', 'is_active', 'order'],
                name='core_tip_locale_active_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['locale', 'text'],
                name='core_tip_unique_per_locale',
            ),
        ]

    def __str__(self):
        return self.text[:60]
