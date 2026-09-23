# backend/apps/master_exams/services/master_exam_attempt_service/question_flow.py
"""
Current-question lookup, navigation, and unanswered iteration.
"""
from apps.questions.models import Question
from apps.questions.payloads import exam_question_payload, snapshot_image_url
from apps.learning.confidence import normalize_confidence


def _next_unanswered(question_ids, answers):
    answered = set(int(k) for k in answers.keys())
    for qid in question_ids:
        if qid not in answered:
            return qid
    return None


def current_question(attempt):
    attempt_qids = list(attempt.question_ids or [])
    current_id = attempt.current_question_id
    if current_id is None or current_id not in attempt_qids:
        current_id = _next_unanswered(
            attempt_qids, attempt.answers,
        )
    if current_id is None:
        current_id = attempt_qids[0] if attempt_qids else None
    if current_id != attempt.current_question_id:
        attempt.current_question_id = current_id
        attempt.save(update_fields=['current_question_id'])

    if current_id is None:
        return None

    try:
        question = (
            Question.objects
            .select_related('case')
            .get(id=current_id)
        )
    except Question.DoesNotExist:
        return None

    payload = exam_question_payload(question)
    snapshot = (attempt.grading_snapshot or {}).get(str(question.id))
    if snapshot:
        payload.update({
            'text': snapshot['question'],
            'choices': snapshot['choices'],
            'translations': snapshot.get('translations') or {},
            'image_url': snapshot_image_url(snapshot),
            'case': (
                {key: snapshot['case'][key] for key in ('id', 'key', 'stem')}
                if snapshot.get('case') else None
            ),
        })
    saved = attempt.answers.get(str(question.id)) or {}
    return {
        'index': attempt_qids.index(question.id),
        'total': len(attempt_qids),
        'question': payload,
        'saved_answer': saved.get('answer'),
        'saved_confidence': normalize_confidence(saved.get('confidence', 3)),
    }


def goto_question(attempt, question_id):
    attempt_qids = list(attempt.question_ids or [])
    if question_id not in attempt_qids:
        raise ValueError('QUESTION_NOT_IN_EXAM')
    attempt.current_question_id = question_id
    attempt.save(update_fields=['current_question_id'])
    return attempt
