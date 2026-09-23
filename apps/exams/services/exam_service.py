# backend/apps/exams/services/exam_service.py

import uuid
from datetime import timedelta
from typing import NamedTuple, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.db.models import Q, F

from apps.users.models import User
from apps.questions.models import Question
from apps.questions.payloads import (
    build_grading_snapshot,
    case_block_from_live,
    case_block_from_snapshot,
    exam_question_payload,
    resolve_choice_ceiling,
    snapshot_image_url,
)
from apps.learning.srs_service import SRSService
from apps.learning.confidence import normalize_confidence, is_confident
from ..models import ExamSession, TestHistory, Blueprint


def _max_choices():
    return getattr(settings, 'MAX_CHOICES', 8)


class AnswerSubmission(NamedTuple):
    """
    Return value for `ExamService.submit_answer`.

    Two fields travel together because they are two halves of the
    same fact — which question was answered, and what the session
    looked like after the write.

    WHY THE ID IS CARRIED OUT OF THE SERVICE
    ----------------------------------------
    Before this type existed, `submit_answer` returned only the
    `ExamSession`, and its one caller — `SubmitAnswerView.post` —
    separately recomputed the answered question id from its own
    PRE-lock snapshot of `session.current_index`. In the sequential
    path the two agreed by construction. Under concurrent
    submissions on the same session (two tabs, a network retry, a
    browser-restore from a second device) the caller's pre-lock
    snapshot could diverge from the row the lock eventually
    serialized, and the study-mode feedback lookup —
    `study_feedback(session, answered_qid, answer)` — would then
    resolve an explanation for a question other than the one the
    answer was actually written against.

    Carrying the id out of the service removes the divergence
    entirely: the id and the session are both read from the same
    `locked` row, inside the same transaction, before any mutation
    of `current_index` runs.

    `answered_qid` is `None` when the submission was not directed
    at a specific question — the current index was out of range
    for this session's `question_ids` list. Callers that also
    gate on `answer is not None` (the only caller does) will
    short-circuit before touching this field in the common case.
    """
    session: ExamSession
    answered_qid: Optional[int]


class ExamService:

    # ═══════════════════════════════════════════════════════════════
    # Answer-slot parsing — single source of truth
    # ═══════════════════════════════════════════════════════════════
    #
    # The `session.answers` JSONField stores one entry per question
    # index. Two shapes exist on disk:
    #
    #   • Modern  — {"answer": int, "confidence": 1|2|3,
    #                "error_reason": str | None}
    #   • Legacy  — a bare int (pre-dict-migration sessions)
    #
    # Before this consolidation, four methods independently branched
    # on `isinstance(raw, dict)`:
    #
    #   • `_saved_answer`      — read the answer index
    #   • `_saved_confidence`  — read the confidence flag
    #   • `_unpack_answer`     — read all three (used by grade_exam)
    #   • an inline block in `get_question`'s study-mode branch
    #
    # `_read_answer_slot` is now the ONE place that decides the
    # dict-or-raw split. The four callers above delegate to it and
    # apply their own semantics for the "missing slot" case.

    @staticmethod
    def _read_answer_slot(raw):
        """
        Parse one stored answer slot.

        Returns (answer, confidence, error_reason) where any of the
        three may be None:

          • raw is None           → (None, None, None)
          • raw is a dict         → the dict's values, with
                                    confidence defaulted to True
          • raw is a bare value   → (raw, None, None)
                                    (legacy pre-dict shape)

        `confidence=None` in the legacy/missing cases is deliberate:
        each caller applies its own policy on top — `_unpack_answer`
        for the grader defaults to True, `_saved_confidence` for the
        API preserves None so the client can distinguish "the user
        was never shown a confidence prompt" from "the user
        confirmed their confidence."
        """
        if raw is None:
            return None, None, None
        if isinstance(raw, dict):
            return (
                raw.get('answer'),
                normalize_confidence(raw.get('confidence', 3)),
                raw.get('error_reason'),
            )
        return raw, None, None

    @staticmethod
    def _unpack_answer(raw):
        """
        Grader-facing wrapper: a missing or legacy slot implies
        confidence=True (the historical default for sessions that
        predate the confidence feature).
        """
        answer, confidence, error_reason = ExamService._read_answer_slot(raw)
        if confidence is None:
            confidence = 3
        return answer, confidence, error_reason

    @staticmethod
    def _saved_answer(session, index):
        raw = session.answers.get(str(index))
        answer, _, _ = ExamService._read_answer_slot(raw)
        return answer

    @staticmethod
    def _saved_confidence(session, index):
        raw = session.answers.get(str(index))
        _, confidence, _ = ExamService._read_answer_slot(raw)
        return confidence

    @staticmethod
    def _saved_pre_answer(session, index):
        raw = session.answers.get(str(index))
        if not isinstance(raw, dict):
            return None
        return raw.get('pre_answer') or None

    @staticmethod
    def start_session(user, mode, question_ids, tag=None, blueprint=None):
        session_id = str(uuid.uuid4())

        with transaction.atomic():
            ExamSession.objects.filter(
                user=user,
                mode=mode,
                is_active=True,
            ).delete()

            session = ExamSession.objects.create(
                session_id=session_id,
                user=user,
                mode=mode,
                question_ids=question_ids,
                tag=tag,
                blueprint=blueprint,
                # Freeze grading inputs before the session is
                # answerable. Any concurrent edit to a Question row
                # after this point does not affect this session.
                grading_snapshot=build_grading_snapshot(question_ids),
            )

        return session

    @staticmethod
    def _snapshot_entry(session, question_id):
        return (session.grading_snapshot or {}).get(str(question_id))

    @staticmethod
    def get_question(session, index):
        if index >= len(session.question_ids):
            return None

        qid = session.question_ids[index]
        snapshot = ExamService._snapshot_entry(session, qid)

        try:
            question = Question.objects.select_related('case').get(id=qid)
        except Question.DoesNotExist:
            question = None

        # Both the snapshot and the live row are gone — this can only
        # happen for a session created before the snapshot column
        # existed, whose question has since been deleted.
        if question is None and not snapshot:
            return None

        if snapshot:
            # Display the frozen payload. A question edited or deleted
            # mid-session still renders identically to how it looked
            # when the candidate opened it.
            #
            # The case block is produced by `case_block_from_snapshot`
            # so it carries the same four keys as every other case
            # block in the codebase (previously it omitted `title`).
            payload = {
                'id': qid,
                'text': snapshot.get('question') or '',
                'choices': snapshot.get('choices') or [],
                'translations': snapshot.get('translations') or {},
                'image_url': snapshot_image_url(snapshot),
                'case': case_block_from_snapshot(snapshot.get('case')),
            }
            payload['verified'] = question.verified if question else False
            payload['verified_by'] = question.verified_by if question else None
        else:
            payload = exam_question_payload(question)
            payload['verified'] = question.verified
            payload['verified_by'] = question.verified_by

        if session.mode in ('study', 'recall'):
            raw = session.answers.get(str(index))
            answer, _, _ = ExamService._read_answer_slot(raw)
            is_answered = answer is not None
            if is_answered:
                if snapshot:
                    payload['explanation'] = snapshot.get('explanation')
                elif question is not None:
                    payload['explanation'] = question.explanation

        saved_pre_answer = ExamService._saved_pre_answer(session, index)
        if session.mode == 'recall' and not saved_pre_answer:
            payload['choices'] = []
            payload['choices_hidden'] = True
            payload['translations'] = {
                locale: {'question': content.get('question', '')}
                for locale, content in (payload.get('translations') or {}).items()
                if isinstance(content, dict)
            }
        else:
            payload['choices_hidden'] = False

        return {
            'index': index,
            'total': len(session.question_ids),
            'question': payload,
            'saved_answer': ExamService._saved_answer(session, index),
            'saved_confidence': ExamService._saved_confidence(session, index),
            'saved_pre_answer': saved_pre_answer,
            'tag': session.tag,
            'blueprint_id': session.blueprint_id,
        }

    @staticmethod
    def submit_answer(
        session, answer, action, target_index=None, confidence=None,
        error_reason=None, pre_answer=None,
    ):
        """
        Record one answer slot and advance the session's current index.

        RETURNS
        -------
        `AnswerSubmission` — a named tuple of `(session, answered_qid)`.
        See the class docstring for why the id travels with the
        session.

        CONCURRENCY
        -----------
        Wrapped in a `select_for_update()` transaction. Without the
        lock, two concurrent submissions on the same session (two
        tabs, a network retry, a browser-restore from a second
        device) would both read the same `answers` JSONField, both
        mutate their own in-memory copy, and both write with
        `save(update_fields=['answers', 'current_index'])`. The
        second write silently clobbered the first — the first
        answer would be gone from the persisted dict even though the
        response for it had already reported success.

        The row lock serializes the read-modify-write at the database
        layer, so the second submission sees the first one's result
        before applying its own mutation.

        ANSWERED-QUESTION ID
        --------------------
        The `answered_qid` in the return value is read from the
        LOCKED row's `current_index`, taken BEFORE the mutation of
        `current_index` runs. That ordering matters: if the id were
        read after the increment/decrement, the id would refer to
        the NEXT question rather than the one that was answered.

        This is the whole reason the field is returned rather than
        left for the caller to recompute — the caller's own
        pre-lock snapshot can diverge from the locked row under
        concurrent submissions, and the study-mode feedback lookup
        must use the id of the question the answer was actually
        written against.

        REJECTION CASES
        ---------------
        • The row is gone (bare `ValueError`) — the session was
          deleted between the caller's fetch and the lock. The
          caller's own `get_object_or_404` makes this rare; the
          check here is the second line of defence.
        """
        with transaction.atomic():
            locked = (
                ExamSession.objects
                .select_for_update()
                .filter(pk=session.pk)
                .first()
            )
            if locked is None:
                raise ValueError('الجلسة غير موجودة أو تم إنهاؤها مسبقاً')

            idx = locked.current_index
            answered_qid = None
            question = None

            if idx < len(locked.question_ids):
                answered_qid = locked.question_ids[idx]
                question = Question.objects.filter(id=answered_qid).first()

            existing_slot = locked.answers.get(str(idx))
            existing_slot = existing_slot if isinstance(existing_slot, dict) else {}
            clean_pre_answer = None
            if pre_answer is not None:
                clean_pre_answer = str(pre_answer).strip()[:1000]

            if (
                answer is not None
                and locked.mode == 'recall'
                and not (clean_pre_answer or existing_slot.get('pre_answer'))
            ):
                raise ValueError('اكتب إجابتك أولاً قبل إظهار الخيارات')

            if answer is not None or clean_pre_answer:
                if isinstance(answer, bool) or not isinstance(answer, int) or answer < 1:
                    raise ValueError('إجابة غير صالحة. يجب أن تكون رقماً موجباً')

                # Choice ceiling — shared with the master-exam grader
                # via `resolve_choice_ceiling`, so the fallback order
                # (snapshot choices → live question choices →
                # MAX_CHOICES) cannot drift between the two graders.
                snap = None
                if idx < len(locked.question_ids):
                    snap = ExamService._snapshot_entry(locked, locked.question_ids[idx])
                max_choice = resolve_choice_ceiling(snap, question)

                if max_choice is not None:
                    if answer > max_choice:
                        raise ValueError(
                            f'إجابة غير صالحة. هذا السؤال يحتوي على {max_choice} خيارات فقط'
                        )
                else:
                    ceiling = _max_choices()
                    if answer > ceiling:
                        raise ValueError(
                            f'إجابة غير صالحة. يجب أن تكون بين 1 و {ceiling}'
                        )

            if answer is not None:
                # NOTE: this in-place mutation only persists because
                # `save(update_fields=['answers', ...])` forces the
                # field to be written. Do NOT drop `'answers'` from
                # the update_fields list — JSONField mutations are
                # invisible to Django's change detection.
                slot = dict(existing_slot)
                if clean_pre_answer:
                    slot['pre_answer'] = clean_pre_answer
                if answer is not None:
                    slot.update({
                        'answer': answer,
                        'confidence': normalize_confidence(confidence),
                        'error_reason': error_reason,
                    })
                locked.answers[str(idx)] = slot

            if action == 'next':
                locked.current_index += 1
            elif action == 'previous' and idx > 0:
                locked.current_index -= 1
            elif action == 'goto' and target_index is not None:
                if 0 <= target_index < len(locked.question_ids):
                    locked.current_index = target_index

            locked.save(update_fields=['answers', 'current_index'])
            return AnswerSubmission(session=locked, answered_qid=answered_qid)

    @staticmethod
    def _merged_question_data(question, snapshot):
        """
        Return the field set `grade_exam` reads, sourced from the
        snapshot when one exists and from the live Question row
        otherwise. Returns None when neither source is available.

        The dict shape is intentionally flat (not the nested payload
        shape) because `grade_exam` only reads individual fields and
        then repackages them into the results list.

        The `case` block is normalized through
        `case_block_from_snapshot` / `case_block_from_live` so a
        session whose snapshot predates the addition of `title`
        still produces the same four-key shape as a fresh one.
        """
        if snapshot:
            return {
                'id': question.id if question is not None else None,
                'correct_answer': snapshot.get('correct_answer'),
                'question': snapshot.get('question'),
                'choices': snapshot.get('choices') or [],
                'explanation': snapshot.get('explanation'),
                'category_id': snapshot.get('category_id'),
                'category_name': snapshot.get('category_name'),
                'category_color': snapshot.get('category_color'),
                'difficulty': snapshot.get('difficulty'),
                'case': case_block_from_snapshot(snapshot.get('case')),
                'image_url': snapshot_image_url(snapshot),
            }
        if question is None:
            return None
        return {
            'id': question.id,
            'correct_answer': question.correct_answer,
            'question': question.question,
            'choices': question.choices or [],
            'explanation': question.explanation,
            'category_id': question.category_id,
            'category_name': question.category.name if question.category else None,
            'category_color': question.category.color if question.category else None,
            'difficulty': question.difficulty,
            'case': case_block_from_live(question.case) if question.case_id else None,
            'image_url': question.image.url if question.image else None,
        }

    @staticmethod
    def grade_exam(question_ids, answers, question_snapshots=None):
        questions = Question.objects.select_related('case', 'category').in_bulk(question_ids)
        correct_count = 0
        present_count = 0
        answered_count = 0
        confidence_correct = 0
        confidence_fragile = 0
        results = []

        for idx, qid in enumerate(question_ids):
            q = questions.get(qid)
            snapshot = (question_snapshots or {}).get(str(qid), {})
            merged = ExamService._merged_question_data(q, snapshot)

            # Neither the live row nor a snapshot exists — only
            # possible for a legacy session predating the snapshot
            # column. Skip, exactly as the previous implementation
            # did.
            if merged is None:
                continue

            correct_answer = merged['correct_answer']

            present_count += 1
            raw = answers.get(str(idx))
            user_ans, confidence_score, error_reason = ExamService._unpack_answer(raw)
            pre_answer = raw.get('pre_answer') if isinstance(raw, dict) else None

            if user_ans is not None:
                answered_count += 1

            is_correct = (user_ans == correct_answer) if user_ans is not None else False

            if is_correct:
                correct_count += 1
                if is_confident(confidence_score):
                    confidence_correct += 1
                else:
                    confidence_fragile += 1

            results.append({
                'question_id': merged['id'] if merged['id'] is not None else qid,
                'question': merged['question'],
                'choices': merged['choices'],
                'image_url': merged['image_url'],
                'correct_answer': correct_answer,
                'user_answer': user_ans,
                'is_correct': is_correct,
                'confidence': is_confident(confidence_score),
                'confidence_score': confidence_score,
                'pre_answer': pre_answer,
                'error_reason': error_reason if not is_correct else None,
                'explanation': merged['explanation'],
                'category_id': merged['category_id'],
                'category_name': merged['category_name'],
                'category_color': merged['category_color'],
                'difficulty': merged['difficulty'],
                'case': merged['case'],
            })

        accuracy = (correct_count / present_count * 100) if present_count else 0

        return {
            'questions': results,
            'correct_count': correct_count,
            'total_questions': present_count,
            'answered_count': answered_count,
            'accuracy': accuracy,
            'confidence_correct': confidence_correct,
            'confidence_fragile': confidence_fragile,
        }

    @staticmethod
    def study_feedback(session, question_id, answer):
        """
        Return (explanation, is_correct) for the study-mode answer
        just submitted. Uses the session's frozen snapshot so mid-
        session edits to the question cannot flip the feedback the
        candidate sees. Falls back to the live row only when the
        snapshot has no entry (legacy sessions).
        """
        if question_id is None:
            return None, None
        snap = ExamService._snapshot_entry(session, question_id)
        if snap:
            explanation = snap.get('explanation')
            correct = snap.get('correct_answer')
        else:
            q = (
                Question.objects
                .filter(id=question_id)
                .only('explanation', 'correct_answer')
                .first()
            )
            if q is None:
                return None, None
            explanation = q.explanation
            correct = q.correct_answer

        if answer is None:
            return explanation, None
        return explanation, (answer == correct)

    @staticmethod
    def update_question_stats(results):
        with transaction.atomic():
            for r in results:
                if r['user_answer'] is None:
                    continue
                # A deleted question cannot be updated. The snapshot
                # still grades correctly; only the stat counters lose
                # the entry, which is the honest outcome — there is no
                # row left to increment.
                if r.get('question_id') is None:
                    continue
                updates = {'times_answered': F('times_answered') + 1}
                if r['is_correct']:
                    updates['times_correct'] = F('times_correct') + 1
                Question.objects.filter(id=r['question_id']).update(**updates)

    @staticmethod
    def record_completion_side_effects(user, results):
        ExamService.update_question_stats(results)
        SRSService.record_attempts_bulk(user, results)
        user.record_study_day()

    @staticmethod
    def finish_session(session, user):

        with transaction.atomic():
            # Re-read under lock. If the row is gone, someone else
            # finished this session concurrently.
            locked = (
                ExamSession.objects
                .select_for_update()
                .filter(pk=session.pk)
                .first()
            )
            if locked is None:
                raise ValueError('الجلسة غير موجودة أو تم إنهاؤها مسبقاً')
            if not locked.is_active:
                raise ValueError('الجلسة غير نشطة ولا يمكن إنهاؤها مرة أخرى')

            # Mark inactive BEFORE doing any write work, so a
            # re-entrant call sees the change even if the delete has
            # not yet run.
            locked.is_active = False
            locked.save(update_fields=['is_active'])

            started_at = locked.started_at or timezone.now()
            total_time = locked.accumulated_time + int(
                (timezone.now() - started_at).total_seconds()
            )

            result = ExamService.grade_exam(
                locked.question_ids,
                locked.answers,
                question_snapshots=locked.grading_snapshot,
            )

            ExamService.record_completion_side_effects(user, result['questions'])
            ExamService.save_history(
                user,
                locked.mode,
                locked.tag,
                result['total_questions'],
                result['correct_count'],
                result['accuracy'],
                total_time,
                started_at,
            )
            # Delete inside the transaction so a mid-flight failure
            # rolls the delete back together with the results rows.
            locked.delete()

        return {
            'results': result['questions'],
            'correct_count': result['correct_count'],
            'total_questions': result['total_questions'],
            'accuracy': result['accuracy'],
            'total_time': total_time,
            'answered_count': result['answered_count'],
            'confidence_correct': result['confidence_correct'],
            'confidence_fragile': result['confidence_fragile'],
        }

    @staticmethod
    def save_history(user, mode, tag, total_questions, correct_count, accuracy, time_spent, started_at=None):
        """
        Write the history row for a finished session. This is now the
        ONLY writer of session history — `save_session_result` was
        removed during the StudySession/TestHistory consolidation.
        """
        history = TestHistory.objects.create(
            user=user,
            mode=mode,
            tag=tag,
            total_questions=total_questions,
            correct_count=correct_count,
            accuracy=accuracy,
            time_spent=time_spent,
            started_at=started_at,
        )
        return history

    @staticmethod
    def pause_session(session):
        """
        Mark a session inactive and fold the elapsed time into
        `accumulated_time`.

        CONCURRENCY
        -----------
        Wrapped in a `select_for_update()` transaction. Without the
        lock, two concurrent pause calls (a double-click, a slow
        network retry, two tabs) would both read the same
        `accumulated_time` and `started_at`, both compute the same
        elapsed interval, and both write the incremented value.
        Depending on timing the interval could be double-counted
        (each writer adds the same elapsed to a different starting
        base and the second write wins), or the second write could
        silently overwrite the first. The row lock serializes the
        read-modify-write, and the terminal check on `is_active` is
        re-run against the locked row so the second call is a no-op.

        The caller's snapshot is returned unchanged if the row is
        gone (the session was concurrently deleted by
        `finish_session`). This mirrors the pre-lock behaviour — the
        view responds 200 with the session id it had, and no write
        occurs. There is nothing to pause.
        """
        with transaction.atomic():
            locked = (
                ExamSession.objects
                .select_for_update()
                .filter(pk=session.pk)
                .first()
            )
            if locked is None:
                return session

            if not locked.is_active:
                return locked

            if locked.started_at:
                locked.accumulated_time += int(
                    (timezone.now() - locked.started_at).total_seconds()
                )
            locked.is_active = False
            locked.save(update_fields=['accumulated_time', 'is_active'])
            return locked

    @staticmethod
    def resume_session(session):
        """
        Transition a paused session back to active.

        CONCURRENCY
        -----------
        Wrapped in a `select_for_update()` transaction. The pre-lock
        version was a bare read-modify-write:

            if session.is_active: return session
            session.is_active = True
            session.started_at = timezone.now()
            session.save(update_fields=['is_active', 'started_at'])

        Two concurrent resumes both read `is_active = False`, both
        set `started_at = now()`, and the second write wins — the
        timer starts marginally later in the persisted row. That
        alone is a minor discrepancy, but the same shape races
        unpredictably against `pause_session`: the final DB state
        depends on write ordering and on which stale snapshot each
        caller held, and is not predictable from the code. The lock
        serializes the two transitions against the same row and
        makes the outcome deterministic.

        The terminal check on `is_active` is re-run against the
        locked row, so a second concurrent resume that acquires the
        lock after the first is a no-op — it returns the
        already-active session without rewriting `started_at`.

        The caller's snapshot is returned unchanged if the row is
        gone (`finish_session` deleted it concurrently). The view
        reads `session.session_id` on the return value; the caller's
        object still carries it, so the response remains well-formed
        and no write occurs.
        """
        with transaction.atomic():
            locked = (
                ExamSession.objects
                .select_for_update()
                .filter(pk=session.pk)
                .first()
            )
            if locked is None:
                return session

            if locked.is_active:
                return locked

            locked.is_active = True
            locked.started_at = timezone.now()
            locked.save(update_fields=['is_active', 'started_at'])
            return locked


class BlueprintService:
    @staticmethod
    def select_question_ids(blueprint, count):
        from django.db.models import Count as DjCount
        from apps.questions.models import Category

        if count <= 0:
            return []

        weights = {
            entry.category_id: entry.weight
            for entry in blueprint.weight_entries.all()
        }

        if not weights:
            return list(
                Question.objects.public()
                .order_by('?')
                .values_list('id', flat=True)[:count]
            )

        valid_cat_ids = set(
            Category.objects
            .filter(id__in=list(weights.keys()))
            .values_list('id', flat=True)
        )

        normalized = {}
        for cid, w in weights.items():
            if cid not in valid_cat_ids:
                continue
            try:
                weight = float(w)
            except (ValueError, TypeError):
                continue
            if weight > 0:
                normalized[cid] = weight

        if not normalized:
            return list(
                Question.objects.public()
                .order_by('?')
                .values_list('id', flat=True)[:count]
            )

        total_weight = sum(normalized.values())
        available = dict(
            Question.objects
            .public()
            .filter(category_id__in=normalized.keys())
            .values_list('category_id')
            .annotate(c=DjCount('id'))
        )

        selected = []
        selected_set = set()
        shortfall = 0

        for cid, w in normalized.items():
            target = int(round(count * w / total_weight))
            if target <= 0:
                continue
            avail = available.get(cid, 0)
            take = min(target, avail)
            if take > 0:
                ids = list(
                    Question.objects
                    .public()
                    .filter(category_id=cid)
                    .order_by('?')
                    .values_list('id', flat=True)[:take]
                )
                selected.extend(ids)
                selected_set.update(ids)
            shortfall += target - take

        if shortfall > 0 and len(selected) < count:
            remaining = count - len(selected)
            extras = list(
                Question.objects
                .public()
                .exclude(id__in=selected_set)
                .order_by('?')
                .values_list('id', flat=True)[:remaining]
            )
            selected.extend(extras)

        import random
        random.shuffle(selected)
        return selected[:count]
