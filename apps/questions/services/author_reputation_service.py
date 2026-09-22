# backend/apps/questions/services/author_reputation_service.py
import logging

from django.db import transaction
from django.db.models import Count, Q

from apps.users.models import User

logger = logging.getLogger(__name__)


class AuthorReputationService:

    @staticmethod
    def refresh_user(user):
        """
        Recompute one user's trust_score and questions_count from
        their authored questions.

        AUTHORSHIP RESOLUTION
        ---------------------
        Trust score is credited to `authored_by` — the field that
        records who originally wrote the content. It is NOT
        `owned_by`: a moderator who takes over someone else's
        questions should not gain reputation for content they did
        not write, and the original author should not lose credit
        when they hand off maintenance.

        See the two-FK docstring on `Question` for the full rationale.

        DEACTIVATED USERS
        -----------------
        This method does not filter on `is_active` or `is_stub`. A
        deactivated user still authored their questions; the count
        and score should still reflect that. Stubs (import-created
        external authors) accumulate a trust score of 0 because
        their questions are unverified — which is the honest state.
        """
        if user is None:
            return False
        changed = AuthorReputationService.refresh_users([user.id])
        user.refresh_from_db(fields=['trust_score', 'questions_count'])
        return user.id in changed

    @staticmethod
    def refresh_users(author_ids):
        """Recompute a group of authors with one aggregate query and one bulk write."""
        if not author_ids:
            return {}
        from apps.questions.models import Question

        users = list(User.objects.filter(id__in=author_ids).only(
            'id', 'trust_score', 'questions_count', 'username',
        ))
        stats = {
            row['authored_by_id']: row
            for row in (
                Question.objects
                .filter(authored_by_id__in=author_ids, is_draft=False)
                .values('authored_by_id')
                .annotate(total=Count('id'), verified=Count('id', filter=Q(verified=True)))
            )
        }
        changed = {}
        to_update = []
        for user in users:
            row = stats.get(user.id) or {}
            total = row.get('total', 0) or 0
            verified = row.get('verified', 0) or 0
            score = round(verified / total * 100, 1) if total else 0.0
            if user.trust_score == score and user.questions_count == total:
                continue
            user.trust_score = score
            user.questions_count = total
            changed[user.id] = (score, total)
            to_update.append(user)
        if to_update:
            User.objects.bulk_update(to_update, ['trust_score', 'questions_count'])
        return changed

    @staticmethod
    @transaction.atomic
    def refresh_all(batch_size=200):
        """
        Recompute every non-admin, non-stub contributor. Iterates in
        id order so a very large user table does not pull the whole
        population into memory in one query.

        Stubs are skipped: they never authored a verified question,
        so their counters are already 0/0 and recomputing them is
        wasted work.
        """
        scanned = 0
        updated = 0
        ids = (
            User.objects
            .exclude(role='admin')
            .exclude(is_stub=True)
            .order_by('id')
            .values_list('id', flat=True)
        )
        batch = []
        for user_id in ids.iterator(chunk_size=batch_size):
            batch.append(user_id)
            if len(batch) >= batch_size:
                scanned += len(batch)
                updated += len(AuthorReputationService.refresh_users(batch))
                batch = []
        if batch:
            scanned += len(batch)
            updated += len(AuthorReputationService.refresh_users(batch))

        logger.info(
            'Author reputation refresh complete: scanned=%d updated=%d',
            scanned, updated,
        )
        return {'scanned': scanned, 'updated': updated}
