# backend/apps/master_exams/services/master_exam_attempt_service/__init__.py
"""
Package surface for MasterExamAttemptService.

Split across:

  • helpers.py        — grace / tolerance / weight helpers
  • start.py          — start, preview, shuffle
  • question_flow.py  — current_question, goto_question
  • answer.py         — submit_answer
  • finish.py         — finish, force-finish
  • status.py         — status payload

`MasterExamAttemptService` is a thin façade that re-exports every
public method as a static method, so existing call sites keep
working unchanged.
"""
from . import start as start_mod
from . import question_flow
from . import answer as answer_mod
from . import finish as finish_mod
from . import status as status_mod


class MasterExamAttemptService:
    # ── Start / preview / shuffle ─────────────────────────────────
    _exam_question_ids = staticmethod(start_mod._exam_question_ids)
    _shuffle_preserving_case_groups = staticmethod(
        start_mod._shuffle_preserving_case_groups
    )
    _start_preview = staticmethod(start_mod._start_preview)
    start = staticmethod(start_mod.start)

    # ── Question flow ─────────────────────────────────────────────
    _next_unanswered = staticmethod(question_flow._next_unanswered)
    current_question = staticmethod(question_flow.current_question)
    goto_question = staticmethod(question_flow.goto_question)

    # ── Answering ─────────────────────────────────────────────────
    submit_answer = staticmethod(answer_mod.submit_answer)

    # ── Finishing ─────────────────────────────────────────────────
    finish = staticmethod(finish_mod.finish)
    _finish_locked = staticmethod(finish_mod._finish_locked)
    _force_finish = staticmethod(finish_mod._force_finish)

    # ── Status ────────────────────────────────────────────────────
    status = staticmethod(status_mod.status)


__all__ = ['MasterExamAttemptService']