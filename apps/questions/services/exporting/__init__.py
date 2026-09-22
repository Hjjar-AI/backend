# backend/apps/questions/services/exporting/__init__.py
"""
Exporting sub-package.

Public façade is `ExportService`, which preserves the original
`apps.questions.services.export_service.ExportService` API. Every
method delegates to a module-level function in one of the sibling
modules:

  • flat_export.py         — XLSX / CSV / JSON
  • state_export.py        — full state envelope (v2)
  • image_export.py        — base64 image encoding
  • formula_sanitizer.py   — spreadsheet formula-injection defense
  • pdf_export.py          — PDF rendering (WeasyPrint)

FILTER + TITLE SUPPORT
----------------------
`export_questions` accepts:

  • `filters` — optional dict of queryset narrowers. Every key is
    optional. See flat_export._build_export_queryset.

  • `title`   — optional document label. PDF export uses it for the
    header banner and the generated filename. Excel, CSV, and JSON
    ignore it: there is no natural place for a document title in a
    flat data dump, and forcing one (e.g. a title row above the
    Excel header) would break the flat-import parser that expects
    the header row on line 1.

When both are omitted, the output is byte-identical to the
pre-feature version of this module.
"""

from .flat_export import export_questions as _export_questions
from .state_export import export_state as _export_state


class ExportService:
    """
    Backward-compatible façade over the exporting sub-modules.

    Every method here delegates to a module-level function in one of
    the sibling modules. The class exists so existing call sites
    (`ExportService.export_questions(...)` in apps/database/views.py,
    tests, scripts) keep working unchanged.
    """

    @staticmethod
    def export_questions(
        fmt='excel', verified_only=False, filters=None, title=None, theme=None,
    ):
        """
        Flat export of the question bank.

        `filters` and `title` are optional and both default to None.
        A caller that passes neither sees the pre-feature behaviour:
        every public question (or every verified question, if
        `verified_only`) written with the default document title.
        """
        return _export_questions(
            fmt=fmt,
            verified_only=verified_only,
            filters=filters,
            title=title,
            theme=theme,
        )

    @staticmethod
    def export_state(include_images=True, verified_only=False):
        """
        Full state envelope (v2). See state_export.export_state.

        NOTE: state export is a full backup of the categories / tags /
        cases / questions graph. Filtering it would produce an
        envelope that cannot be round-tripped into a working system,
        and a document title has no place in a machine-readable
        backup. Neither `filters` nor `title` is accepted here.
        """
        return _export_state(
            include_images=include_images,
            verified_only=verified_only,
        )


__all__ = ['ExportService']
