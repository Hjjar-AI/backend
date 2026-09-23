"""Duplicate marking and question-bank quality reporting."""

import re
import secrets
import string
from collections import defaultdict

from apps.feedback.services import FeedbackService

from ..models import (
    Question,
    CHOICE_TEXT_MAX_LENGTH,
    QUESTION_TEXT_MAX_LENGTH,
)


QUALITY_FLAG_PREFIX = '[DATA_QUALITY]'
DUPLICATE_MARKER_RE = re.compile(r'\s*\[DUP-[A-Z]{4}\]\s*$', re.IGNORECASE)


def quality_key(value):
    value = DUPLICATE_MARKER_RE.sub('', str(value or ''))
    return ' '.join(value.split()).casefold()


def _marker():
    return '[DUP-' + ''.join(secrets.choice(string.ascii_uppercase) for _ in range(4)) + ']'


def _marked(value, max_length):
    suffix = f' {_marker()}'
    base = DUPLICATE_MARKER_RE.sub('', str(value)).rstrip()
    return f'{base[:max_length - len(suffix)].rstrip()}{suffix}'


def prepare_import_content(question_text, choices, seen_question_keys, *, check_question=True):
    """Mark accepted duplicates and return ``(question, choices, issues)``."""
    issues = []
    question_text = str(question_text)
    original_key = quality_key(question_text)
    already_seen = (
        seen_question_keys.get(original_key, 0) > 0
        if hasattr(seen_question_keys, 'get')
        else original_key in seen_question_keys
    )
    if check_question and already_seen:
        question_text = _marked(question_text, QUESTION_TEXT_MAX_LENGTH)
        issues.append('duplicate_question')
    if original_key:
        if hasattr(seen_question_keys, 'get'):
            seen_question_keys[original_key] += 1
        else:
            seen_question_keys.add(original_key)

    marked_choices = []
    seen_choices = set()
    for index, choice in enumerate(choices, start=1):
        key = quality_key(choice)
        if key and key in seen_choices:
            marked_choices.append(_marked(choice, CHOICE_TEXT_MAX_LENGTH))
            issues.append(f'duplicate_choice:{index}')
        else:
            marked_choices.append(choice)
        if key:
            seen_choices.add(key)
    return question_text, marked_choices, issues


def flag_import_quality_issues(question, acting_user, issues):
    if not issues or acting_user is None:
        return False
    labels = []
    if 'duplicate_question' in issues:
        labels.append('duplicate question text was accepted and marked')
    duplicate_choices = [
        issue.split(':', 1)[1]
        for issue in issues
        if issue.startswith('duplicate_choice:')
    ]
    if duplicate_choices:
        labels.append(
            'duplicate choices were accepted and marked at positions '
            + ', '.join(duplicate_choices)
        )
    reason = f'{QUALITY_FLAG_PREFIX} ' + '; '.join(labels)
    return FeedbackService.flag_question(
        acting_user.id,
        question.id,
        reason=reason,
    )


def build_data_quality_report():
    questions = list(
        Question.objects
        .select_related('category')
        .prefetch_related('flags')
        .order_by('id')
    )
    groups = defaultdict(list)
    for question in questions:
        key = quality_key(question.question)
        if key:
            groups[key].append(question.id)

    items = []
    counts = defaultdict(int)
    for question in questions:
        issues = []
        key = quality_key(question.question)
        if key and len(groups[key]) > 1:
            issues.append({
                'code': 'duplicate_question',
                'related_question_ids': groups[key],
            })

        seen_choices = set()
        duplicate_positions = []
        for index, choice in enumerate(question.choices or [], start=1):
            choice_key = quality_key(choice)
            if choice_key in seen_choices:
                duplicate_positions.append(index)
            seen_choices.add(choice_key)
        if duplicate_positions:
            issues.append({
                'code': 'duplicate_choices',
                'positions': duplicate_positions,
            })
        if not (question.explanation or '').strip():
            issues.append({'code': 'missing_explanation'})
        if question.category_id is None:
            issues.append({'code': 'missing_category'})
        if not (question.source_document or '').strip():
            issues.append({'code': 'missing_source_document'})

        translation_issues = []
        invalid_translation_locales = []
        translations = question.translations or {}
        if not isinstance(translations, dict):
            invalid_translation_locales.append('*')
            translations = {}
        for locale, content in translations.items():
            if not isinstance(content, dict) or not str(
                content.get('question') or ''
            ).strip():
                invalid_translation_locales.append(str(locale))
                continue
            translated_choices = content.get('choices') or []
            if not isinstance(translated_choices, list):
                invalid_translation_locales.append(str(locale))
                continue
            if translated_choices and len(translated_choices) != len(question.choices or []):
                translation_issues.append(locale)
        if invalid_translation_locales:
            issues.append({
                'code': 'invalid_translations',
                'locales': invalid_translation_locales,
            })
        if translation_issues:
            issues.append({
                'code': 'translation_choice_count_mismatch',
                'locales': translation_issues,
            })

        if not issues:
            continue
        for issue in issues:
            counts[issue['code']] += 1
        has_open_quality_flag = any(
            not flag.resolved
            and (flag.reason or '').startswith(QUALITY_FLAG_PREFIX)
            for flag in question.flags.all()
        )
        items.append({
            'id': question.id,
            'uuid': str(question.uuid),
            'question': question.question,
            'issues': issues,
            'has_open_quality_flag': has_open_quality_flag,
        })

    return {
        'summary': {
            'questions_scanned': len(questions),
            'questions_with_issues': len(items),
            'issue_counts': dict(counts),
        },
        'items': items,
    }


def flag_data_quality_report(report, acting_user):
    created = 0
    by_id = {question.id: question for question in Question.objects.filter(
        id__in=[item['id'] for item in report.get('items') or []]
    )}
    for item in report.get('items') or []:
        if item.get('has_open_quality_flag'):
            continue
        codes = ', '.join(issue['code'] for issue in item.get('issues') or [])
        question = by_id.get(item['id'])
        if question and FeedbackService.flag_question(
            acting_user.id,
            question.id,
            reason=f'{QUALITY_FLAG_PREFIX} {codes}',
        ):
            created += 1
    return created
