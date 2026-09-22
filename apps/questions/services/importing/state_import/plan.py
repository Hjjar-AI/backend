"""Pure question-entry checks shared by state preview and write passes."""

from ..validators import MAX_CHOICES
from uuid import UUID


def canonical_uuid(raw):
    if not raw:
        return ''
    value = str(raw).strip()
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        return value


def prepared_question(entry):
    uuid_str = canonical_uuid(entry.get('uuid'))
    question = (entry.get('question') or '').strip()
    if not uuid_str or not question:
        return None
    choices = [
        str(choice).strip()
        for choice in (entry.get('choices') or [])
        if choice is not None and str(choice).strip()
    ][:MAX_CHOICES]
    if len(choices) < 2:
        return None
    try:
        correct_answer = int(entry.get('correct_answer', 1))
    except (TypeError, ValueError):
        return None
    if not 1 <= correct_answer <= len(choices):
        return None
    return {
        'uuid': uuid_str,
        'question': question,
        'choices': choices,
        'correct_answer': correct_answer,
        'is_draft': bool(entry.get('is_draft', False)),
    }
