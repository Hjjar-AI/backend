# backend/apps/master_exams/services/master_exam_service/audience.py
"""
Audience resolution and per-user access checks for MasterExam.

TWO DIRECTIONS, DELIBERATELY DIFFERENT STATUS FILTERS
-----------------------------------------------------
This module answers two different questions, each in its natural
direction:

  • `resolve_audience(exam)` — "who is in this exam's audience?"
    (exam → people). Used by `user_can_access` and by the results
    dashboard's `total_assigned` counter.

  • `_assigned_exam_ids(user)` — "which exams is this user
    assigned to?" (person → exams). Used by `needs_acknowledgement`
    and by the "my exams" list.

They share the same three-predicate membership rule (directly
assigned, member of an assigned group, or in the `audience_all_
doctors` population) but they filter on `stored_status` differently:

  • `resolve_audience` does NOT filter by status. Membership in an
    exam's audience is a fact about the exam's audience, not about
    the exam's current lifecycle state. A user who was in the
    audience of an exam that later became `published_to_bank` is
    still in the audience of that exam — the exam is just archived.
    This matters for `user_can_access`, which must keep working on
    an archived exam for anyone who could see it while it was live.

  • `_assigned_exam_ids` DOES filter out `draft`, `cancelled`, and
    `published_to_bank`. The consumer is a UI list ("my exams",
    "my pending acknowledgements") and none of those three states
    belongs there: a draft is not assigned to anyone, a cancelled
    exam is withdrawn, and a bank-published exam is archived.

Prior to this revision the difference was undocumented and looked
like drift. It is now documented, and the two directions share the
one predicate helper — `_all_doctors_q()` for the exam → people
direction and `_is_all_doctors_user(user)` for the person → exams
direction. Same rule, one place each.
"""
import logging

from django.db.models import Q
from django.utils import timezone

from apps.groups.models import GroupMembership
from apps.users.models import User

from ...models import (
    MasterExam,
    MasterExamAttempt,
    MasterExamAcknowledgement,
)

logger = logging.getLogger(__name__)


# ── All-doctors predicate — single source of truth ────────────────────
#
# Both directions of the audience computation need to answer the same
# question: "is this user (or: which users are) part of the
# `audience_all_doctors` population?" The previous revision answered
# it twice, in two different mechanisms — a Python attribute check in
# `_assigned_exam_ids` and a SQL filter in `resolve_audience`. They
# happened to agree, but a future change to the population definition
# (e.g. adding `role='moderator'` or removing the `is_stub` exclusion)
# had to be made in two unrelated places.

_ALL_DOCTORS_ROLE = 'member'


def _is_all_doctors_user(user):
    """
    True when `user` is in the `audience_all_doctors` population.

    Python-side predicate, used by the person → exams direction.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    return (
        user.role == _ALL_DOCTORS_ROLE
        and user.is_active
        and not user.is_stub
    )


def _all_doctors_q():
    """
    Q object matching every User in the `audience_all_doctors`
    population. SQL-side predicate, used by the exam → people
    direction.

    Kept in lockstep with `_is_all_doctors_user` above by construction
    — both reference `_ALL_DOCTORS_ROLE` and the same three field
    checks. If the population definition changes, both change.
    """
    return Q(role=_ALL_DOCTORS_ROLE, is_active=True, is_stub=False)


def resolve_audience(exam):
    """
    Every user who is in this exam's audience, excluding the primary
    attending (who is the author of record and is not a participant).

    The audience is the union of three predicates:
      • Directly assigned users          → audience_users
      • Members of any assigned group    → audience_groups → memberships
      • Every active non-stub member     → audience_all_doctors

    No `stored_status` filter — see the module docstring for why.

    Both `id__in` clauses below pass a LAZY queryset, not a
    materialized list. Django compiles a queryset RHS into a SQL
    subquery, so the outer `SELECT` on the `users` table ends up
    shaped as:

        WHERE id IN (SELECT user_id FROM group_memberships WHERE …)

    rather than the previous

        WHERE id IN (1, 2, 3, …, N)

    The difference matters when a group has thousands of members:
    the materialized form builds an `IN` clause with one literal per
    member, which both loses the index-driven pushdown and can
    exceed MariaDB's `max_allowed_packet` for a sufficiently large
    audience. Reordering to use the subquery form keeps the plan
    index-friendly at any audience size.
    """
    q = Q(id__in=exam.audience_users.values_list('id', flat=True))

    group_ids = list(exam.audience_groups.values_list('id', flat=True))
    if group_ids:
        # NOTE: pass the queryset directly to `__in`. Do NOT call
        # `list(...)` on it — that is what forced the wide IN list
        # this comment exists to prevent.
        member_ids = GroupMembership.objects.filter(
            group_id__in=group_ids
        ).values_list('user_id', flat=True)
        q |= Q(id__in=member_ids)

    if exam.audience_all_doctors:
        q |= _all_doctors_q()

    return User.objects.filter(q).exclude(id=exam.primary_attending_id).distinct()


def user_can_access(user, exam):
    if user is None or not user.is_authenticated:
        return False
    if user.has_capability('master_exams.manage_any'):
        return True
    if user.id == exam.primary_attending_id:
        return True
    if exam.co_attendings.filter(id=user.id).exists():
        return True
    return resolve_audience(exam).filter(id=user.id).exists()


def user_has_attempt(user, exam):
    return MasterExamAttempt.objects.filter(
        master_exam=exam, user=user, is_complete=False,
    ).first()


def user_completed_attempt(user, exam):
    return MasterExamAttempt.objects.filter(
        master_exam=exam, user=user, is_complete=True,
    ).first()


def acknowledge(exam, user):
    MasterExamAcknowledgement.objects.get_or_create(
        user=user, master_exam=exam,
    )


def needs_acknowledgement(user):
    now = timezone.now()
    assigned = _assigned_exam_ids(user)
    if not assigned:
        return MasterExam.objects.none()
    acked = MasterExamAcknowledgement.objects.filter(
        user=user,
    ).values_list('master_exam_id', flat=True)
    completed = MasterExamAttempt.objects.filter(
        user=user, is_complete=True,
    ).values_list('master_exam_id', flat=True)
    return (
        MasterExam.objects
        .filter(id__in=assigned, closes_at__gt=now)
        .exclude(id__in=acked)
        .exclude(id__in=completed)
        .exclude(stored_status__in=('draft', 'cancelled'))
    )


def _assigned_exam_ids(user):
    """
    Return the ids of exams this user is assigned to, for the UI
    list views.

    Status filter — the reason this differs from `resolve_audience`
    is documented in the module docstring. Short version: a UI list
    ("my exams") should not show drafts (not assigned to anyone),
    cancelled exams (withdrawn), or bank-published exams (archived).
    A viewer-facing membership check (`user_can_access`) uses the
    unfiltered `resolve_audience` direction instead.
    """
    if user is None or not user.is_authenticated:
        return []
    q_direct = Q(audience_users__id=user.id)
    q_group = Q(audience_groups__memberships__user_id=user.id)
    q_all = Q(pk__in=[])
    if _is_all_doctors_user(user):
        q_all = Q(audience_all_doctors=True)
    return list(
        MasterExam.objects
        .filter(q_direct | q_group | q_all)
        .exclude(primary_attending_id=user.id)
        .exclude(stored_status__in=('draft', 'cancelled', 'published_to_bank'))
        .values_list('id', flat=True)
        .distinct()
    )