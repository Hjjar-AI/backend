# backend/apps/questions/services/importing/__init__.py
"""
Importing sub-package.

Public façade is `ImportService`, which preserves the original
`apps.questions.services.import_service.ImportService` API. Every
method delegates to a module-level function in one of the sibling
modules:

  • validators.py         — shared MIME / value validation
  • flat_import.py        — XLSX / XLS / CSV / JSON
  • telegram_import.py    — Telegram poll exports
  • state_import.py       — full state envelope (v2)
  • author_resolution.py  — envelope author attribution
  • image_ingest.py       — base64 image attachment
"""

from .flat_import import import_file as _import_file
from .telegram_import import import_telegram as _import_telegram
from .state_import import (
    import_state as _import_state,
    STATE_IMPORT_MODE_MERGE,
)


class ImportService:
    """
    Backward-compatible façade over the importing sub-modules.

    Every method here delegates to a module-level function in one of
    the sibling modules. The class exists so existing call sites
    (`ImportService.import_file(...)` in `apps/database/views.py`,
    tests, scripts) keep working unchanged after the file was split
    into a package.

    The default value of `mode` on `import_state` is the
    `STATE_IMPORT_MODE_MERGE` constant, not a `'merge'` literal.
    Both evaluate to the same string today; using the constant means
    the façade cannot drift from the domain constant if the string
    is ever changed.
    """

    @staticmethod
    def import_file(file, username):
        """Flat XLSX / XLS / CSV / JSON import. See flat_import.import_file."""
        return _import_file(file, username)

    @staticmethod
    def import_telegram(file, username):
        """Telegram poll-export import. See telegram_import.import_telegram."""
        return _import_telegram(file, username)

    @staticmethod
    def import_state(
        file,
        username,
        mode=STATE_IMPORT_MODE_MERGE,
        dry_run=False,
        analyze=False,
        mapping=None,
    ):
        """
        Full state-envelope import (v2). See state_import.import_state
        for the analyze / dry_run / merge / replace semantics.
        """
        return _import_state(
            file,
            username,
            mode=mode,
            dry_run=dry_run,
            analyze=analyze,
            mapping=mapping,
        )


__all__ = ['ImportService']