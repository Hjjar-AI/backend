"""Shared, conservative mastery scoring for learner-facing reports."""

from collections import defaultdict

from django.db.models import Count

from apps.questions.models import Category, Question

from .models import UserQuestionAttempt


def attempt_mastery_score(attempt):
    """Return 0..100 evidence for one question's current learning state.

    A current lapse is always zero. A correct first exposure cannot by itself
    cross the 80% mastery threshold: confidence contributes at most 15 points
    and spaced successful repetitions contribute the remaining 35.
    """
    if not attempt.last_correct:
        return 0.0
    confidence = max(1, min(3, attempt.last_confidence_score or 3))
    confidence_points = confidence * 5.0
    repetition_points = min(max(attempt.repetitions or 0, 0), 3) / 3.0 * 35.0
    return min(100.0, 50.0 + confidence_points + repetition_points)


def mastery_summary(attempts, total_questions):
    """Score a scope with unseen questions included as zero evidence."""
    attempts = list(attempts)
    attempted = len(attempts)
    denominator = max(int(total_questions or 0), attempted)
    score = (
        sum(attempt_mastery_score(attempt) for attempt in attempts) / denominator
        if denominator else 0.0
    )
    return {
        'score': round(score, 1),
        'attempted_questions': attempted,
        'coverage': round((attempted / denominator) * 100, 1) if denominator else 0.0,
        'attempts': sum(attempt.attempts or 0 for attempt in attempts),
        'wrong_count': sum(attempt.wrong_count or 0 for attempt in attempts),
    }


def category_mastery_rows(user_id, min_attempts=1):
    categories = list(Category.objects.all())
    totals = dict(
        Question.objects.public()
        .filter(category__isnull=False)
        .values('category_id')
        .annotate(total=Count('id'))
        .values_list('category_id', 'total')
    )
    grouped = defaultdict(list)
    for attempt in (
        UserQuestionAttempt.objects
        .filter(
            user_id=user_id,
            question__category__isnull=False,
            question__is_draft=False,
        )
        .select_related('question')
    ):
        grouped[attempt.question.category_id].append(attempt)

    rows = []
    for category in categories:
        attempts = grouped.get(category.id, [])
        summary = mastery_summary(attempts, totals.get(category.id, 0))
        if summary['attempts'] < min_attempts:
            continue
        rows.append({
            'category_id': category.id,
            'category_name': category.name,
            'category_color': category.color or '#667eea',
            'distinct_questions': summary['attempted_questions'],
            'total_questions': totals.get(category.id, 0),
            'coverage': summary['coverage'],
            'attempts': summary['attempts'],
            'wrong_count': summary['wrong_count'],
            # Keep the established API key while making its semantics the
            # same mastery score used by the knowledge map.
            'accuracy': summary['score'],
            'mastery_score': summary['score'],
            'mastered': summary['score'] >= 80.0,
        })
    return rows
