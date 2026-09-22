# backend/apps/questions/services/exporting/pdf_export.py
"""
PDF export path.

Renders the question bank to a print-ready PDF via WeasyPrint, which
drives Pango/HarfBuzz for text shaping. That is what makes Arabic
contextual letter-joining and RTL bidi reordering work correctly
without an arabic-reshaper / python-bidi preprocessing layer.

The Arabic font is embedded into the PDF from
`frontend/public/fonts/NotoSansArabic-VariableFont_wdth,wght.ttf`,
so the generated file carries its own glyphs and does not depend on
the reader's system having an Arabic font installed.

VISUAL LANGUAGE
---------------
The stylesheet below mirrors the app's own token palette from
`frontend/src/assets/tokens.css`. No bright whites anywhere.

PER-CATEGORY COLOURS (no inline styles)
---------------------------------------
The category-coloured dot next to each question's category name uses
a CSS class per category rather than an inline `style` attribute.
Rules are generated from the Category rows referenced by the exported
question list and appended to the stylesheet.

CUSTOM TITLE
------------
`export_questions_pdf` accepts an optional `title` argument. When
provided, it becomes the `<h1>` of the PDF header banner AND the
descriptive segment of the generated filename. When omitted, the
default title is used and the filename falls back to the
timestamp-only pattern.

TITLE SECURITY
--------------
The title flows into two places:

  • The HTML template, where Django's autoescape turns any `<` into
    `&lt;` — no XSS risk.
  • The generated filename, where `_sanitize_filename_part` strips
    path separators and control characters, collapses whitespace,
    and caps the result at 60 characters. Django's FileResponse
    handles the RFC 5987 encoding for non-ASCII names.

F-STRING SAFETY NOTE
--------------------
The stylesheet below is an f-string. Every CSS brace is doubled
(`{{` / `}}`) so Python does not try to interpolate it. Because the
Django template syntax `{% ... %}` contains bare braces, it MUST NOT
appear anywhere inside an f-string in this module — not even inside
a CSS comment. Any comment that needs to reference template syntax
should describe it in prose instead.

INPUT CONTRACT
--------------
`export_questions_pdf(questions, ...)` receives an already-materialized
list of Question objects. The list is built by
`flat_export._build_export_queryset` with the right select_related /
prefetch_related / filter set already applied.

RETURN CONTRACT
---------------
  • {'filepath', 'filename'} on success
  • {'error', 'code'} on failure

CSV-PARAM PARSING (fix — delegated to a shared helper)
------------------------------------------------------
`_split_csv` used to be a hand-written re-implementation of the same
`raw.split(',') → strip → drop empties` sequence the question-list
and available-count views use. It now delegates to
`apps.core.utils.parse_csv_param` so a future change to the parsing
rule lands in one place.
"""
import logging
import re
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone
from apps.core.artifacts import reserve_artifact_path, download_filename
from apps.core.fonts import resolve_arabic_font_path
from apps.core.utils import parse_csv_param
from apps.questions.colors import is_valid_hex_color
from ...models import Question

logger = logging.getLogger(__name__)


_VALID_DIFFICULTIES = tuple(value for value, _ in Question.DIFFICULTY_CHOICES)

_DIFFICULTY_LABELS_AR = {
    'easy': 'سهل',
    'medium': 'متوسط',
    'hard': 'صعب',
}

# Default document title when no custom title is supplied. Matches
# the Arabic UI language of the PDF template. Localizing this to the
# reader's active locale is a separate feature — see the module
# docstring for why the PDF is Arabic-only today.
_DEFAULT_TITLE = 'بنك الأسئلة'

# Fallback when a Category row has an empty `color` field.
_DEFAULT_CATEGORY_COLOR = '#c47d3a'

# Maximum title length. A longer title is truncated (with an ellipsis)
# rather than rejected. The same cap is applied at the view layer so
# the value the admin sees in the input field matches what will
# actually land in the PDF.
_MAX_TITLE_LENGTH = 150

# Characters that are hostile on any filesystem. Stripped from the
# descriptive segment of the generated filename. Null and C0 control
# characters are also stripped.
_FILENAME_HOSTILE_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def _safe_color(raw):
    """
    Return `raw` if it is a well-formed hex colour, otherwise the
    fallback.
    """
    if is_valid_hex_color(raw):
        return raw.strip()
    return _DEFAULT_CATEGORY_COLOR


def _sanitize_filename_part(raw, max_length=60):
    """
    Turn a human title into a filesystem-safe filename segment.

    Strips path separators, colons, and other characters that are
    illegal on Windows or confusing on Unix, collapses runs of
    whitespace into single underscores, and drops leading/trailing
    underscores and dots (a leading dot would make the file hidden
    on Unix; a trailing dot is illegal on Windows).

    Arabic and other non-ASCII characters are preserved — Django's
    FileResponse emits the filename using RFC 5987 (`filename*=UTF-8''…`)
    which every modern browser understands.

    Returns '' when nothing usable remains.
    """
    if not raw:
        return ''
    cleaned = _FILENAME_HOSTILE_RE.sub('', str(raw))
    cleaned = re.sub(r'\s+', '_', cleaned)
    cleaned = cleaned.strip('._')
    return cleaned[:max_length]


def _split_csv(raw):
    """
    Split a raw filter value into a list of non-empty stripped strings.

    Delegates to `apps.core.utils.parse_csv_param` — the same helper
    that the question-list and available-count views use for their
    `category_ids` and `tags_filter` query params. Before this
    revision, this function was a hand-written re-implementation of
    the same `raw.split(',') → strip → drop empties` sequence, which
    meant a change to the parsing rule (e.g. allowing semicolons as
    separators, or trimming mixed whitespace) had to land in two
    unrelated files.

    A non-string `raw` (e.g. an already-parsed list from a direct
    caller that bypasses the query-string layer) is handled by
    `parse_csv_param` itself — it treats any non-string iterable as
    already-parsed and stringifies each element.
    """
    return parse_csv_param(raw)


def _build_filters_summary(filters):
    """
    Human-readable descriptions of the active filters, for the PDF
    header. Empty list when no filters are active.
    """
    if not filters:
        return []

    from ...models import Category, Tag

    parts = []

    search = (filters.get('search') or '').strip()
    if len(search) >= 2:
        parts.append(f'بحث: «{search}»')

    raw_difficulties = _split_csv(filters.get('difficulty'))
    valid = [
        d.lower() for d in raw_difficulties
        if d.lower() in _VALID_DIFFICULTIES
    ]
    if valid:
        labels = [_DIFFICULTY_LABELS_AR.get(d, d) for d in valid]
        parts.append(f'الصعوبة: {"، ".join(labels)}')

    cat_ids_raw = _split_csv(filters.get('category_ids'))
    ids = []
    for piece in cat_ids_raw:
        try:
            value = int(piece)
        except (TypeError, ValueError):
            continue
        if value > 0:
            ids.append(value)
    if ids:
        names = list(
            Category.objects
            .filter(id__in=ids)
            .order_by('name')
            .values_list('name', flat=True)
        )
        if names:
            parts.append(f'التصنيف: {"، ".join(names)}')

    raw_tag = (filters.get('tag') or '').strip()
    if raw_tag and Tag.objects.filter(name=raw_tag).exists():
        parts.append(f'الوسم: {raw_tag}')

    tag_names = _split_csv(filters.get('tags_filter'))
    if tag_names:
        existing = list(
            Tag.objects
            .filter(name__in=tag_names)
            .order_by('name')
            .values_list('name', flat=True)
        )
        if existing:
            parts.append(f'الوسوم: {"، ".join(existing)}')

    return parts


def _build_category_color_css(questions):
    """
    Generate one CSS rule per unique category referenced by the
    exported questions:

        .q-category-dot--cat-3 { background-color: #11998e; }

    Called from `export_questions_pdf` and appended to the stylesheet
    so the HTML template can use a class of the form
    `q-category-dot--cat-<id>` instead of an inline `style`
    attribute.
    """
    seen = {}
    for q in questions:
        if q.category_id is None or q.category is None:
            continue
        if q.category_id in seen:
            continue
        seen[q.category_id] = _safe_color(q.category.color)

    if not seen:
        return ''

    lines = ['/* Per-category dot colours */']
    for cid, color in sorted(seen.items()):
        lines.append(
            f'.q-category-dot--cat-{cid} {{ background-color: {color}; }}'
        )
    return '\n'.join(lines)


def _resolve_doc_title(title, verified_only):
    """
    Resolve the final title string for the PDF header banner.

    A custom title wins outright — the "verified only" suffix is NOT
    appended, because the admin who typed the title is assumed to
    have chosen their wording deliberately.

    When no title is supplied, the default is used and the "verified
    only" suffix IS appended if applicable.

    The result is length-capped. A title longer than the cap is
    truncated with an ellipsis character rather than rejected —
    refusing to export because the title was 160 characters instead
    of 150 would be hostile.
    """
    if title:
        resolved = str(title).strip()
        if len(resolved) > _MAX_TITLE_LENGTH:
            resolved = resolved[: _MAX_TITLE_LENGTH - 1].rstrip() + '…'
        return resolved

    resolved = _DEFAULT_TITLE
    if verified_only:
        resolved += ' — الأسئلة المدققة فقط'
    return resolved


def export_questions_pdf(
    questions,
    *,
    verified_only=False,
    filters=None,
    title=None,
):
    """
    Render the given question list as a PDF.

    `questions` is a materialized list of Question objects, pre-loaded
    with the FKs and tags the template reads.

    `title` is optional. When supplied, it replaces the default header
    banner text and becomes the descriptive segment of the generated
    filename. When omitted, the default title is used.
    """
    try:
        from weasyprint import HTML, CSS
    except ImportError:
        logger.error(
            'WeasyPrint is not installed; PDF export is unavailable. '
            'Install it with: pip install weasyprint'
        )
        return {
            'error': 'محرك PDF غير مثبت على الخادم. تواصل مع المسؤول.',
            'code': 500,
        }

    if not questions:
        return {'error': 'لا توجد بيانات للتصدير', 'code': 404}

    # Attach the Arabic difficulty label as a transient attribute.
    for q in questions:
        q.difficulty_label = _DIFFICULTY_LABELS_AR.get(
            q.difficulty, q.difficulty,
        )

    font_path = resolve_arabic_font_path()
    font_uri = font_path.as_uri() if font_path else None
    if font_path is None:
        logger.warning(
            'Arabic TTF not found in FRONTEND_DIR/public/fonts or '
            'BASE_DIR/static/fonts — PDF will use a system fallback font.'
        )

    filters_summary = _build_filters_summary(filters)
    category_color_css = _build_category_color_css(questions)
    doc_title = _resolve_doc_title(title, verified_only)

    html_string = render_to_string(
        'exports/questions_pdf.html',
        {
            'questions': questions,
            'verified_only': verified_only,
            'exported_at': timezone.now(),
            'filters_summary': filters_summary,
            'doc_title': doc_title,
        },
    )

    if font_uri:
        font_face_css = f"""
        @font-face {{
            font-family: 'Noto Arabic';
            src: url('{font_uri}') format('truetype-variations');
            font-weight: 100 900;
            font-style: normal;
        }}
        """
    else:
        font_face_css = ''

    # ── Stylesheet ────────────────────────────────────────────────
    #
    # This is an f-string. Every literal CSS brace is doubled
    # (`{{` / `}}`). The only single-brace tokens below are the
    # interpolation slots: `{font_face_css}`, `{_DEFAULT_CATEGORY_COLOR}`
    # and `{category_color_css}`.
    #
    # DO NOT paste `{% ... %}` (Django template syntax) anywhere
    # inside this f-string, including inside CSS comments — Python
    # will try to evaluate it as an interpolation expression and
    # raise SyntaxError. Describe template syntax in prose instead.
    stylesheet = CSS(string=f"""
        {font_face_css}

        @page {{
            size: A4;
            margin: 1.4cm 1.2cm 1.6cm 1.2cm;
            background: #f5ede0;

            @bottom-center {{
                content: counter(page) ' / ' counter(pages);
                font-size: 9pt;
                color: #8b7355;
                font-family: 'Noto Arabic', sans-serif;
            }}
            @bottom-right {{
                content: 'مُختبِر';
                font-size: 8pt;
                color: #a08c70;
                font-family: 'Noto Arabic', sans-serif;
            }}
        }}

        html, body {{
            font-family: 'Noto Arabic', sans-serif;
            direction: rtl;
            text-align: right;
            font-size: 10.5pt;
            line-height: 1.7;
            color: #3b2a1f;
            background: #f5ede0;
            margin: 0;
            padding: 0;
        }}

        /* ── Document header banner ──────────────────────────── */
        .doc-header {{
            margin: 0 0 1em 0;
            padding: 0.85em 1em;
            background: linear-gradient(135deg, #c47d3a 0%, #a86a2e 100%);
            color: #fdf6ea;
            border-radius: 6px;
        }}

        .doc-header h1 {{
            font-size: 18pt;
            margin: 0 0 0.15em 0;
            font-weight: 700;
            color: #fdf6ea;
            letter-spacing: -0.01em;
            line-height: 1.35;
        }}

        .doc-meta {{
            margin: 0;
            font-size: 9pt;
            color: #f0dcc2;
        }}

        .doc-meta strong {{
            color: #fdf6ea;
            font-weight: 600;
        }}

        /* ── Filters summary strip ───────────────────────────── */
        .filters-applied {{
            font-size: 9pt;
            color: #5a4a38;
            background: #e8dcc4;
            border-inline-start: 4px solid #c47d3a;
            padding: 0.5em 0.8em;
            border-radius: 4px;
            margin: 0 0 1.2em 0;
            line-height: 1.6;
        }}

        .filters-applied strong {{
            color: #8a5420;
            font-weight: 700;
        }}

        /* ── Question card ───────────────────────────────────── */
        .question {{
            page-break-inside: avoid;
            background: #eae0d0;
            border: 1px solid #d4c4a8;
            border-inline-start: 4px solid #c9b89c;
            border-radius: 6px;
            padding: 0.85em 0.95em 0.75em 0.95em;
            margin-bottom: 0.85em;
        }}

        .question--easy   {{ border-inline-start-color: #558b6f; }}
        .question--medium {{ border-inline-start-color: #d49a1f; }}
        .question--hard   {{ border-inline-start-color: #b55a4a; }}

        /* ── Question header row ─────────────────────────────── */
        .q-header {{
            display: flex;
            align-items: center;
            margin: 0 0 0.55em 0;
        }}

        .q-number {{
            display: inline-block;
            min-width: 1.7em;
            text-align: center;
            padding: 0 7px;
            background: #3b2a1f;
            color: #f5ede0;
            border-radius: 4px;
            font-size: 9pt;
            font-weight: 700;
            line-height: 1.55;
            margin-inline-end: 6px;
        }}

        .q-difficulty {{
            display: inline-block;
            padding: 1px 9px;
            border-radius: 10px;
            font-size: 8.5pt;
            font-weight: 700;
            margin-inline-end: 6px;
            line-height: 1.6;
        }}

        .q-difficulty--easy {{
            background: #dbe7de;
            color: #3d6b52;
        }}
        .q-difficulty--medium {{
            background: #f1e2c0;
            color: #8a6410;
        }}
        .q-difficulty--hard {{
            background: #eed6d0;
            color: #8a3f34;
        }}

        .q-category {{
            display: inline-flex;
            align-items: center;
            font-size: 9pt;
            color: #8b7355;
            margin-inline-start: auto;
        }}

        /* The default background is the fallback colour. A per-
           category rule below overrides it for every category that
           appears in this export. A question with no category skips
           the dot entirely via the template's conditional block, so
           the fallback only ever fires when a category exists but
           its colour field is malformed. */
        .q-category-dot {{
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-inline-end: 5px;
            border: 1px solid rgba(59, 42, 31, 0.15);
            background-color: {_DEFAULT_CATEGORY_COLOR};
        }}

        /* ── Per-category dot colours ─────────────────────────
           Generated from the categories referenced by this export.
           No inline `style` attributes appear in the HTML. See
           `_build_category_color_css` above. */
        {category_color_css}

        /* ── Question text ───────────────────────────────────── */
        .q-text {{
            font-weight: 600;
            font-size: 11pt;
            color: #3b2a1f;
            line-height: 1.65;
            margin: 0 0 0.7em 0;
        }}

        /* ── Case stem panel ─────────────────────────────────── */
        .case-stem {{
            background: #e1e8ed;
            border-inline-start: 3px solid #2e8ac0;
            padding: 0.55em 0.8em;
            margin: 0 0 0.65em 0;
            border-radius: 4px;
            font-size: 9.5pt;
            color: #3a4d5c;
            line-height: 1.65;
        }}

        .case-stem-label {{
            display: block;
            font-weight: 700;
            color: #2e8ac0;
            font-size: 8.5pt;
            margin-bottom: 0.2em;
            letter-spacing: 0.02em;
        }}

        /* ── Choices ─────────────────────────────────────────── */
        .q-choices {{
            margin: 0 0 0.55em 0;
            padding: 0;
            list-style: none;
        }}

        .q-choice {{
            display: flex;
            align-items: flex-start;
            padding: 0.35em 0.65em;
            margin-bottom: 4px;
            border-radius: 4px;
            background: #f2ebdd;
            border: 1px solid #d9cbb2;
            font-size: 10pt;
            color: #3b2a1f;
            line-height: 1.55;
        }}

        .q-choice:last-child {{
            margin-bottom: 0;
        }}

        .q-choice--correct {{
            background: #dce8dd;
            border-color: #558b6f;
        }}

        .choice-marker {{
            display: inline-block;
            min-width: 20px;
            height: 20px;
            text-align: center;
            border-radius: 50%;
            background: #3b2a1f;
            color: #f5ede0;
            font-size: 8.5pt;
            font-weight: 700;
            line-height: 20px;
            flex-shrink: 0;
            margin-inline-end: 8px;
            margin-top: 1px;
        }}

        .q-choice--correct .choice-marker {{
            background: #558b6f;
            color: #f0f8f1;
        }}

        .choice-text {{
            flex: 1;
            min-width: 0;
        }}

        .choice-check {{
            color: #558b6f;
            font-weight: 700;
            font-size: 11pt;
            flex-shrink: 0;
            margin-inline-start: 6px;
        }}

        /* ── Explanation ─────────────────────────────────────── */
        .q-explanation {{
            margin: 0.6em 0 0 0;
            padding: 0.6em 0.85em;
            background: #efe6d6;
            border-inline-start: 3px solid #c47d3a;
            border-radius: 4px;
            font-size: 9.5pt;
            color: #4a3a2c;
            line-height: 1.65;
        }}

        .q-explanation strong {{
            color: #8a5420;
            font-weight: 700;
        }}

        /* ── Footer / metadata ───────────────────────────────── */
        .q-footer {{
            margin-top: 0.6em;
            padding-top: 0.4em;
            border-top: 1px dashed #c9b89c;
            font-size: 8.5pt;
            color: #8b7355;
            line-height: 1.6;
        }}

        .q-meta-item {{
            display: inline;
            margin-inline-end: 14px;
        }}

        .q-meta-label {{
            color: #5a4a38;
            font-weight: 600;
        }}
    """)

    export_dir = Path(settings.EXPORT_FOLDER)
    export_dir.mkdir(parents=True, exist_ok=True)
    prefix = 'verified_questions_export' if verified_only else 'questions_export'

    # Filename: insert the title's filesystem-safe slug between the
    # prefix and the timestamp when a custom title was given. The
    # empty-title case keeps the original timestamp-only pattern, so
    # a caller that does not set a title sees no filename change.
    title_part = _sanitize_filename_part(title) if title else ''
    artifact_prefix = f'{prefix}_{title_part}' if title_part else prefix
    filepath = reserve_artifact_path(export_dir, artifact_prefix, '.pdf')
    filename = download_filename(filepath)

    try:
        HTML(string=html_string).write_pdf(
            str(filepath),
            stylesheets=[stylesheet],
        )
    except Exception as e:
        logger.exception('Failed to render question-bank PDF: %s', e)
        if filepath.exists():
            try:
                filepath.unlink()
            except OSError:
                pass
        return {'error': 'فشل إنشاء ملف PDF', 'code': 500}

    logger.info('PDF export created: %s (%d questions)', filepath, len(questions))
    return {'filepath': str(filepath), 'filename': filename}