# backend/apps/feedback/services.py

from django.db import IntegrityError, transaction

from .models import Bookmark, QuestionFlag


class FeedbackService:

    @staticmethod
    def toggle_bookmark(user_id, question_id):
        bookmark, created = Bookmark.objects.get_or_create(
            user_id=user_id, question_id=question_id,
        )
        if not created:
            bookmark.delete()
            return False
        return True

    @staticmethod
    def flag_question(user_id, question_id, reason=None, master_exam_attempt=None):
        """
        Create an open flag, or return False if one already exists.

        The `unique_open_flag_per_user_question` constraint on
        QuestionFlag is a *partial* unique index (condition on
        resolved=False). PostgreSQL and SQLite enforce it; MariaDB
        silently drops it and Django emits models.W036 at check time.

        Because MariaDB cannot be relied on to reject the duplicate,
        the guard is done here with a pre-check. On MariaDB two
        concurrent requests can both pass the pre-check and both
        create a flag — a genuinely rare race for a feature where the
        worst case is the moderator sees two identical flags and
        resolves both. On PostgreSQL/SQLite the constraint still fires
        and the IntegrityError handler catches the race.
        """
        if QuestionFlag.objects.filter(
            user_id=user_id,
            question_id=question_id,
            resolved=False,
        ).exists():
            return False

        try:
            with transaction.atomic():
                QuestionFlag.objects.create(
                    user_id=user_id,
                    question_id=question_id,
                    reason=reason,
                    master_exam_attempt=master_exam_attempt,
                )
            return True
        except IntegrityError:
            # Fires on PostgreSQL/SQLite only — MariaDB has no
            # constraint to violate.
            return False