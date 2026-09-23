# backend/apps/questions/services/importing/state_import/__init__.py
"""
Package surface for question-bank package imports (current format v3).

The implementation is split across:

  • constants.py    — format identifiers and mode names
  • identity.py     — uuid-or-key resolution for ported entities
  • validation.py   — envelope shape validation
  • preview.py      — dry-run counters and unknown-author analysis
  • apply.py        — the write pass
  • entrypoint.py   — import_state() (validation → preview → apply)

The parent package's `ImportService.import_state` façade continues
to import `import_state` and `STATE_IMPORT_MODE_MERGE` from this
package's top level — both names are re-exported here.
"""
from .constants import (
    STATE_FORMAT,
    STATE_FORMAT_VERSION,
    STATE_IMPORT_MODE_MERGE,
    STATE_IMPORT_MODE_REPLACE,
    VALID_STATE_IMPORT_MODES,
)
from .entrypoint import import_state

__all__ = [
    'STATE_FORMAT',
    'STATE_FORMAT_VERSION',
    'STATE_IMPORT_MODE_MERGE',
    'STATE_IMPORT_MODE_REPLACE',
    'VALID_STATE_IMPORT_MODES',
    'import_state',
]
