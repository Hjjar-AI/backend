# backend/apps/master_exams/views/common.py
"""
Shared authorization helpers for the master-exam view modules.

Three predicates, each answering a distinct question:

  _is_author(user, exam)
      Is the caller a primary or co-attending of this specific exam?
      No capability check — pure object-level identity.

  _can_manage_exam(user, exam)
      May the caller edit / publish / cancel / reorder / add-draft on
      this specific exam? True if the caller holds 'manage_any', or
      holds 'manage_own' AND is an author of this exam.

  _can_view_results(user, exam)
      May the caller view the full results dashboard for this exam?
      True if the caller holds 'view_results_any', or can manage the
      exam, or holds 'view_results_own' AND is an author.

The second and third are separate because a deployment could
legitimately grant a role the ability to see results without the
ability to edit — the split costs nothing now and avoids a
capability migration later.
"""


def _is_author(user, exam):
    """
    Identity-only test. The exam has two authorship relations:
      • primary_attending    — a single FK.
      • co_attendings        — an M2M.
    Both grant the same permissions; the distinction is display-only
    in the UI (the primary is the "owner of record").
    """
    if user is None:
        return False
    if user.id == exam.primary_attending_id:
        return True
    return exam.co_attendings.filter(id=user.id).exists()


def _can_manage_exam(user, exam):
    """
    Whether the caller may edit / publish / cancel / reorder / add
    drafts to this exam.

    Short-circuit order matters: check 'manage_any' first so an
    admin or super-moderator passes regardless of the exam's
    author list.
    """
    if not user or not user.is_authenticated:
        return False
    if user.has_capability('master_exams.manage_any'):
        return True
    if not user.has_capability('master_exams.manage_own'):
        return False
    return _is_author(user, exam)


def _can_view_results(user, exam):
    """
    Whether the caller may open the results dashboard and download
    the CSV exports for this exam.
    """
    if not user or not user.is_authenticated:
        return False
    if user.has_capability('master_exams.view_results_any'):
        return True
    if _can_manage_exam(user, exam):
        # Anyone who can manage the exam can view its results.
        return True
    if not user.has_capability('master_exams.view_results_own'):
        return False
    return _is_author(user, exam)