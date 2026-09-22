# apps/questions/services/exporting/formula_sanitizer.py


_FORMULA_TRIGGER_CHARS = ('=', '+', '-', '@', '\t', '\r')


def sanitize_formula_cell(value):
    """Neutralize spreadsheet formula-prefix characters (XLSX/CSV only)."""
    if value is None:
        return value
    if not isinstance(value, str):
        return value
    if not value:
        return value

    # Strip ONLY ASCII spaces for the check. Stripping all whitespace
    # would remove the leading \t / \r this function exists to catch.
    stripped = value.lstrip(' ')
    if stripped and stripped[0] in _FORMULA_TRIGGER_CHARS:
        return "'" + value
    return value