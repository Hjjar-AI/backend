"""Validation and normalization for portable multilingual question content."""

import re

from .models import (
    CHOICE_TEXT_MAX_LENGTH,
    EXPLANATION_TEXT_MAX_LENGTH,
    QUESTION_TEXT_MAX_LENGTH,
)


LOCALE_PATTERN = re.compile(r'^[a-z]{2,3}(?:-[A-Z]{2})?$')
TRANSLATION_FIELDS = frozenset({'question', 'choices', 'explanation'})


def normalize_translations(value, *, max_choices=8):
    """Return a normalized translations dict or raise ``ValueError``."""
    if value in (None, ''):
        return {}
    if not isinstance(value, dict):
        raise ValueError('translations must be an object keyed by locale')

    normalized = {}
    for raw_locale, raw_content in value.items():
        locale_parts = str(raw_locale).strip().replace('_', '-').split('-')
        if len(locale_parts) not in {1, 2}:
            raise ValueError(f'Invalid translation locale: {raw_locale!r}')
        locale = locale_parts[0].lower()
        if len(locale_parts) == 2:
            locale += f'-{locale_parts[1].upper()}'
        if not LOCALE_PATTERN.fullmatch(locale):
            raise ValueError(f'Invalid translation locale: {raw_locale!r}')
        if not isinstance(raw_content, dict):
            raise ValueError(f'Translation {locale} must be an object')
        unknown = set(raw_content) - TRANSLATION_FIELDS
        if unknown:
            raise ValueError(
                f'Translation {locale} has unsupported fields: '
                f'{", ".join(sorted(unknown))}'
            )

        raw_question = raw_content.get('question')
        raw_explanation = raw_content.get('explanation')
        if raw_question is not None and not isinstance(raw_question, str):
            raise ValueError(f'Translation {locale} question must be a string')
        if raw_explanation is not None and not isinstance(raw_explanation, str):
            raise ValueError(f'Translation {locale} explanation must be a string')
        question = (raw_question or '').strip()
        explanation = (raw_explanation or '').strip()
        choices = raw_content.get('choices') or []
        if not isinstance(choices, list) or any(
            not isinstance(choice, str) for choice in choices
        ):
            raise ValueError(f'Translation {locale} choices must be a list of strings')
        choices = [choice.strip() for choice in choices if choice.strip()]

        if not question and not explanation and not choices:
            continue
        if not question:
            raise ValueError(f'Translation {locale} question is required')
        if len(question) > QUESTION_TEXT_MAX_LENGTH:
            raise ValueError(f'Translation {locale} question is too long')
        if explanation and len(explanation) > EXPLANATION_TEXT_MAX_LENGTH:
            raise ValueError(f'Translation {locale} explanation is too long')
        if choices and not 2 <= len(choices) <= max_choices:
            raise ValueError(
                f'Translation {locale} must contain 2 to {max_choices} choices'
            )
        if any(len(choice) > CHOICE_TEXT_MAX_LENGTH for choice in choices):
            raise ValueError(f'Translation {locale} contains a choice that is too long')

        normalized[locale] = {
            'question': question,
            'choices': choices,
            'explanation': explanation,
        }
    return normalized
