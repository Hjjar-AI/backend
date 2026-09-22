# backend/apps/questions/validation.py
"""
Shared validation for a question's choice list and correct-answer index.

Every write path that can create or update a question needs the same
four rules applied to `choices` + `correct_answer`:

  • 2 ≤ number of filled choices ≤ MAX_CHOICES
  • no choice exceeds CHOICE_TEXT_MAX_LENGTH
  • no two choices are case-insensitively identical
  • 1 ≤ correct_answer ≤ number of filled choices

Before this module existed, that block was copy-pasted — with the
same Arabic error strings, the same iteration order, and the same
`seen` set pattern — into four places:

  • QuestionCreateSerializer.validate
  • QuestionUpdateSerializer.validate
  • MasterExamDraftCreateSerializer.validate
  • flat_import._build_question

The copies drifted only in how they raised the error
(`serializers.ValidationError` vs. `ValueError`) and in whether the
correct-answer failure was field-scoped. The shared function returns
a structured error and lets each caller raise in the shape its layer
expects.

The function is deliberately pure: no Django model imports at import
time beyond what CHOICE_TEXT_MAX_LENGTH already exposes, and no
settings read at import time beyond what MAX_CHOICES already exposes.
It can be called from a DRF serializer, a plain management command,
or a unit test without a request context.

CORRECT-ANSWER OPTIONALITY
--------------------------
`correct_answer=None` means "do not run the range check". This
preserves the pre-refactor behaviour of QuestionUpdateSerializer,
whose validate() skipped the check when `self.instance` was None
(an unusual but allowed shape in a partial update). Every other
caller passes a concrete integer and gets the range check.

FIELD-SCOPED VS. PLAIN ERRORS
-----------------------------
The `field` key on the returned error dict is `'correct_answer'`
only for the range failure, and only as a *hint*: callers that
field-scope their DRF errors (the draft serializer) read it; callers
that raise plain strings (the create/update serializers, the
importers) ignore it. This preserves the shapes the four original
call sites had, even though they were inconsistent with each other.

CORRECT-ANSWER RANGE — SINGLE SOURCE OF TRUTH
---------------------------------------------
The correct-answer range failure message used to be hardcoded in
four places (this module, flat_import._build_question,
QuestionUpdateSerializer.validate, and Question.save()). The public
`validate_correct_answer()` helper below is now the one place the
message lives. Every call site — including the model's `save()`
guard — delegates to it, so a message change lands once. The
English-only message that used to live in `Question.save()` was
also replaced with the canonical Arabic one so that the same business
rule produces the same error regardless of the entry point.
"""

from django.conf import settings

from .models import CHOICE_TEXT_MAX_LENGTH


MIN_CHOICES = 2
MAX_CHOICES = getattr(settings, 'MAX_CHOICES', 8)


def validate_correct_answer(correct_answer, num_choices):
    """
    Validate a 1-based correct-answer index against a choice count.

    Returns None on success, or a `{'field': 'correct_answer',
    'message': <localized>}` dict on failure. The message is the
    single source of truth for this rule — callers MUST NOT
    re-serialize the string themselves.

    `correct_answer=None` is accepted and returns None, so the caller
    can pass a possibly-absent value through without a pre-check.
    """
    if correct_answer is None:
        return None
    if correct_answer < 1 or correct_answer > num_choices:
        return {
            'field': 'correct_answer',
            'message': (
                f'رقم الإجابة الصحيحة يجب أن يكون بين 1 و {num_choices}'
            ),
        }
    return None


def clean_and_validate_choices(choices, correct_answer=None, max_choices=None):
    """
    Normalize and validate a question's choice list and correct-answer
    index.

    `choices` may be a list of strings and blank values; blanks and
    non-strings are dropped before validation. `correct_answer` is a
    1-based index into the *cleaned* list, or None to skip the range
    check entirely.

    Returns
    -------
    (cleaned_choices, None)
        On success. `cleaned_choices` is the list with whitespace
        stripped, blanks dropped, and no further mutation applied.
    (None, error)
        On failure. `error` is a dict::

            {'field': str | None, 'message': str}

        `field` is `'correct_answer'` only for the correct-answer-range
        failure — the draft serializer uses it to raise a field-scoped
        `ValidationError`; every other caller surfaces `message` verbatim.
        All other failures have `field=None`.
    """
    if max_choices is None:
        max_choices = MAX_CHOICES

    # 1. Strip + drop blanks.
    cleaned = [c.strip() for c in choices if c and c.strip()]

    # 2. Choice count floor.
    if len(cleaned) < MIN_CHOICES:
        return None, {
            'field': None,
            'message': f'يجب أن يكون عدد الخيارات {MIN_CHOICES} على الأقل',
        }

    # 3. Choice count ceiling.
    if len(cleaned) > max_choices:
        return None, {
            'field': None,
            'message': f'يجب أن يكون عدد الخيارات {max_choices} على الأكثر',
        }

    # 4. Per-choice length.
    for c in cleaned:
        if len(c) > CHOICE_TEXT_MAX_LENGTH:
            return None, {
                'field': None,
                'message': (
                    f'الخيار يتجاوز الحد الأقصى ({CHOICE_TEXT_MAX_LENGTH} حرفاً). '
                    f'الطول الحالي: {len(c)}'
                ),
            }

    # 5. Case-insensitive duplicate check.
    seen = set()
    for c in cleaned:
        lower = c.lower()
        if lower in seen:
            return None, {
                'field': None,
                'message': f'يوجد خيار مكرر: {c}',
            }
        seen.add(lower)

    # 6. Correct-answer range (skipped when None).
    error = validate_correct_answer(correct_answer, len(cleaned))
    if error is not None:
        return None, error

    return cleaned, None