"""Build the authenticated user's mastery map by knowledge object."""

from collections import defaultdict

from apps.questions.models import KnowledgeObject, Question

from .models import UserQuestionAttempt


def _attempt_mastery(attempt):
    """Transparent 0-100 score from correctness, confidence, and retention."""
    correctness = 1.0 if attempt.last_correct else 0.0
    confidence = max(1, min(3, attempt.last_confidence_score or 3)) / 3.0
    retention = min(max(attempt.repetitions or 0, 0), 3) / 3.0
    return (correctness * 0.60 + confidence * 0.25 + retention * 0.15) * 100


def build_knowledge_map(user):
    objects = list(
        KnowledgeObject.objects
        .filter(status='active', questions__is_draft=False)
        .select_related('category')
        .distinct()
        .order_by('category__name', 'title')
    )
    if not objects:
        return {
            'summary': {
                'total_objects': 0, 'started_objects': 0,
                'mastered_objects': 0, 'developing_objects': 0,
                'needs_work_objects': 0, 'unstarted_objects': 0,
                'average_mastery': 0,
            },
            'items': [],
        }

    object_ids = [obj.id for obj in objects]
    question_counts = defaultdict(int)
    question_ids = defaultdict(list)
    for row in (
        Question.objects
        .filter(knowledge_object_id__in=object_ids, is_draft=False)
        .values('knowledge_object_id', 'id')
    ):
        question_counts[row['knowledge_object_id']] += 1
        question_ids[row['knowledge_object_id']].append(row['id'])

    grouped = defaultdict(list)
    attempts = (
        UserQuestionAttempt.objects
        .filter(
            user=user,
            question__knowledge_object_id__in=object_ids,
            question__is_draft=False,
        )
        .select_related('question')
    )
    for attempt in attempts:
        grouped[attempt.question.knowledge_object_id].append(attempt)

    items = []
    status_counts = defaultdict(int)
    started_scores = []
    for obj in objects:
        object_attempts = grouped.get(obj.id, [])
        attempted_questions = len(object_attempts)
        total_questions = question_counts[obj.id]
        if object_attempts:
            mastery = round(
                sum(_attempt_mastery(attempt) for attempt in object_attempts)
                / attempted_questions,
            )
            average_confidence = round(
                sum(attempt.last_confidence_score for attempt in object_attempts)
                / attempted_questions,
                2,
            )
            last_answered_at = max(
                attempt.last_answered_at for attempt in object_attempts
            )
            if mastery >= 80:
                status = 'mastered'
            elif mastery >= 50:
                status = 'developing'
            else:
                status = 'needs_work'
            started_scores.append(mastery)
        else:
            mastery = 0
            average_confidence = None
            last_answered_at = None
            status = 'unstarted'

        status_counts[status] += 1
        items.append({
            'id': obj.id,
            'uuid': str(obj.uuid),
            'title': obj.title,
            'learning_objective': obj.learning_objective,
            'category_id': obj.category_id,
            'category_name': obj.category.name if obj.category else None,
            'questions_count': total_questions,
            'question_ids': question_ids[obj.id],
            'attempted_questions': attempted_questions,
            'coverage': round(
                (attempted_questions / total_questions) * 100
                if total_questions else 0,
            ),
            'total_attempts': sum(attempt.attempts for attempt in object_attempts),
            'last_correct_questions': sum(
                1 for attempt in object_attempts if attempt.last_correct
            ),
            'average_confidence': average_confidence,
            'mastery_score': mastery,
            'status': status,
            'last_answered_at': (
                last_answered_at.isoformat() if last_answered_at else None
            ),
            'last_revised_at': obj.last_revised_at.isoformat(),
        })

    return {
        'summary': {
            'total_objects': len(items),
            'started_objects': len(started_scores),
            'mastered_objects': status_counts['mastered'],
            'developing_objects': status_counts['developing'],
            'needs_work_objects': status_counts['needs_work'],
            'unstarted_objects': status_counts['unstarted'],
            'average_mastery': (
                round(sum(started_scores) / len(started_scores))
                if started_scores else 0
            ),
        },
        'items': items,
    }
