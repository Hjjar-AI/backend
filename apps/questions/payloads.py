# backend/apps/questions/payloads.py
"""Question shape shared by regular and master exam runners.

CANONICAL CASE BLOCK
--------------------
The case block that travels with a question payload used to be
hand-built in four places — ``exam_question_payload``,
``build_grading_snapshot``, ``_merged_question_data``'s live branch,
and ``ExamService.get_question``'s snapshot branch — with two
different shapes. Two of the four omitted ``title``; the others
included it. That drift was invisible until someone compared the
four side by side.

The two helpers below — ``case_block_from_live`` and
``case_block_from_snapshot`` — are now the ONE place that decides
the shape. Both return the same four-key dict (``id``, ``key``,
``title``, ``stem``); missing fields are ``None``. Every builder in
this module and every caller that produces a case block delegates
to them.

CHOICE CEILING
--------------
``resolve_choice_ceiling`` centralizes the "prefer the frozen
snapshot's choice list, else fall back to the live question" rule
that both exam graders (``ExamService.submit_answer`` and
``MasterExamAttemptService.submit_answer``) need. Extracting it
means a future change to the fallback order lands in one place
instead of two unrelated files that don't share code.
"""

from .models import Question


# ── Canonical case block ──────────────────────────────────────────────


def case_block_from_live(case):
    """
    Canonical case block from a live ``ClinicalCase`` instance (or
    the FK's cached attribute on a question).

    Returns ``None`` when ``case`` is ``None``, which is what a
    question without a case carries.
    """
    if case is None:
        return None
    return {
        'id': case.id,
        'key': case.key,
        'title': case.title,
        'stem': case.stem,
    }


def case_block_from_snapshot(snap):
    """
    Canonical case block from a frozen snapshot's ``case`` dict (or
    ``None`` when the question has no case).
    """
    if not snap:
        return None
    return {
        'id': snap.get('id'),
        'key': snap.get('key'),
        'title': snap.get('title'),
        'stem': snap.get('stem'),
    }


# ── Choice ceiling ────────────────────────────────────────────────────


def resolve_choice_ceiling(snapshot, question):
    """
    Return the maximum valid 1-based choice index for a question.

    Prefers the frozen snapshot's choice list when it is present; falls
    back to the live ``Question.choices`` list. Returns ``None`` when
    neither source carries a list — the caller then applies its own
    ceiling (e.g. ``MAX_CHOICES``), which is the correct behavior for
    a legacy session whose question has been deleted.

    Both graders (``ExamService.submit_answer`` in apps/exams and
    ``MasterExamAttemptService.submit_answer`` in
    apps/master_exams) call this so the fallback order cannot drift.
    """
    if snapshot:
        snap_choices = snapshot.get('choices')
        if snap_choices is not None:
            return len([c for c in snap_choices if c])
    if question is not None:
        q_choices = question.choices
        if q_choices is not None:
            return len([c for c in q_choices if c])
    return None


# ── Snapshot construction ─────────────────────────────────────────────


def build_grading_snapshot(question_ids):
    """
    Freeze the question data consumed by both exam graders.

    Output shape (unchanged from the previous revision):

        { str(question_id): {
              'correct_answer', 'difficulty',
              'category_id', 'category_name', 'category_color',
              'question', 'choices', 'explanation', 'translations',
              'image_name',
              'case': {'id', 'key', 'title', 'stem'} | None,
        }, ... }

    The ``case`` block is produced by ``case_block_from_live``, so its
    shape is identical to every other case block in the codebase.
    """
    return {
        str(question.id): {
            'correct_answer': question.correct_answer,
            'difficulty': question.difficulty,
            'category_id': question.category_id,
            'category_name': question.category.name if question.category else None,
            'category_color': question.category.color if question.category else None,
            'question': question.question,
            'choices': question.choices,
            'explanation': question.explanation,
            'translations': question.translations or {},
            'image_name': question.image.name if question.image else None,
            'case': case_block_from_live(question.case) if question.case_id else None,
        }
        for question in (
            Question.objects
            .filter(id__in=question_ids)
            .select_related('category', 'case')
        )
    }


def snapshot_image_url(snapshot):
    name = snapshot.get('image_name')
    if not name:
        return None
    try:
        return Question._meta.get_field('image').storage.url(name)
    except (OSError, ValueError):
        return None


def exam_question_payload(question):
    """
    Standalone (non-snapshot) payload for a question, used by both
    exam runners when no frozen snapshot entry exists.

    The ``case`` block is produced by ``case_block_from_live``. Note
    that this adds ``title`` to the block relative to the previous
    revision of this function — that field was already present in
    the snapshot-side blocks, and adding it here removes the drift.
    The addition is backward-compatible: a frontend that does not
    read ``case.title`` simply ignores it.
    """
    return {
        'id': question.id,
        'text': question.question,
        'choices': question.choices,
        'translations': question.translations or {},
        'image_url': question.image.url if question.image else None,
        'case': case_block_from_live(question.case) if question.case_id else None,
    }
