"""Shared validation for category colors."""

import re

HEX_COLOR_RE = re.compile(
    r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$'
)


def is_valid_hex_color(value):
    return isinstance(value, str) and bool(HEX_COLOR_RE.fullmatch(value.strip()))
