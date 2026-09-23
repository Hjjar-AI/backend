"""Shared confidence-score compatibility helpers.

New clients send 1 (guessing), 2 (uncertain), or 3 (confident).  Older
clients sent booleans, so True maps to 3 and False maps to 2.
"""


def normalize_confidence(value, default=3):
    if value is None:
        return default
    if value is True:
        return 3
    if value is False:
        return 2
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return default
        try:
            value = int(value)
        except ValueError as exc:
            raise ValueError('Confidence must be 1, 2, or 3.') from exc
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 3:
        return value
    raise ValueError('Confidence must be 1, 2, or 3.')


def is_confident(value):
    return normalize_confidence(value) == 3
