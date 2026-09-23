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
from .image_export import read_image_as_base64

logger = logging.getLogger(__name__)


_VALID_DIFFICULTIES = tuple(value for value, _ in Question.DIFFICULTY_CHOICES)

_PDF_COPY = {
    'ar': {
        'direction': 'rtl', 'text_align': 'right',
        'default_title': 'بنك الأسئلة',
        'verified_suffix': '— الأسئلة المدققة فقط',
        'difficulty': {'easy': 'سهل', 'medium': 'متوسط', 'hard': 'صعب'},
        'exported_at': 'تم التصدير في', 'question_count': 'عدد الأسئلة',
        'filters': 'الفلاتر المطبقة', 'search': 'بحث',
        'difficulty_label': 'الصعوبة', 'category': 'التصنيف',
        'tag': 'الوسم', 'tags': 'الوسوم',
        'clinical_case': 'حالة سريرية', 'explanation': 'الشرح',
        'source': 'المصدر', 'about_title': 'من نحن',
        'question_image_alt': 'صورة السؤال', 'brand': 'مُختبِر',
    },
    'en': {
        'direction': 'ltr', 'text_align': 'left',
        'default_title': 'Question Bank',
        'verified_suffix': '— Verified questions only',
        'difficulty': {'easy': 'Easy', 'medium': 'Medium', 'hard': 'Hard'},
        'exported_at': 'Exported at', 'question_count': 'Questions',
        'filters': 'Applied filters', 'search': 'Search',
        'difficulty_label': 'Difficulty', 'category': 'Category',
        'tag': 'Tag', 'tags': 'Tags', 'clinical_case': 'Clinical case',
        'explanation': 'Explanation', 'source': 'Source',
        'about_title': 'About Us', 'question_image_alt': 'Question image',
        'brand': 'Mukhtabir',
    },
}

# Fallback when a Category row has an empty `color` field.
_DEFAULT_CATEGORY_COLOR = '#c47d3a'

# The browser keeps its theme choice in localStorage, so it is not available
# to Django implicitly. ExportButtons sends the active theme as a query
# parameter and this registry supplies the print renderer with the matching
# core tokens from frontend/src/assets/tokens.css.
_DEFAULT_PDF_THEME = 'stone'
_PDF_THEME_ALIASES = {'light': _DEFAULT_PDF_THEME}
_PDF_THEME_TOKENS = {
    'stone': {
        'primary': '#7a3f20', 'success': '#2b6547',
        'danger': '#913a32', 'warning': '#755200', 'info': '#285e78',
        'bg_body': '#c4b29f', 'bg_card': '#d9cab9', 'bg_alt': '#cbbbaa',
        'text_primary': '#2d211a', 'text_secondary': '#50382d',
        'text_muted': '#574035', 'on_accent': '#eee3d6',
        'strong_weight': 0.70,
    },
    'dark': {
        'primary': '#e6a15b', 'success': '#68b68b',
        'danger': '#df7769', 'warning': '#e6cf45', 'info': '#64afd2',
        'bg_body': '#0d1218', 'bg_card': '#18212a', 'bg_alt': '#222d38',
        'text_primary': '#e2ddd5', 'text_secondary': '#c9c1b6',
        'text_muted': '#a9a196', 'on_accent': '#26180c',
        'strong_weight': 0.92,
    },
    'onyx': {
        'primary': '#e6a15b', 'success': '#68b68b',
        'danger': '#df7769', 'warning': '#e6cf45', 'info': '#64afd2',
        'bg_body': '#000000', 'bg_card': '#0a0a0a', 'bg_alt': '#151515',
        'text_primary': '#e2ddd5', 'text_secondary': '#c9c1b6',
        'text_muted': '#a9a196', 'on_accent': '#26180c',
        'strong_weight': 0.92,
    },
    'blossom': {
        'primary': '#9b285c', 'success': '#24614f',
        'danger': '#a82746', 'warning': '#775000', 'info': '#315f92',
        'bg_body': '#d2a8bb', 'bg_card': '#e2b9cb', 'bg_alt': '#d6a6bb',
        'text_primary': '#351622', 'text_secondary': '#5d293d',
        'text_muted': '#66364a', 'on_accent': '#f1e2e9',
        'strong_weight': 0.70,
    },
    'fresh': {
        'primary': '#176b42', 'success': '#1f754c',
        'danger': '#9b3f37', 'warning': '#745600', 'info': '#17657c',
        'bg_body': '#aed0b6', 'bg_card': '#c4ddc8', 'bg_alt': '#b2d1b8',
        'text_primary': '#12291c', 'text_secondary': '#294c37',
        'text_muted': '#355840', 'on_accent': '#e4f0e6',
        'strong_weight': 0.70,
    },
    'contrast': {
        'primary': '#1d3a8f', 'success': '#104d28',
        'danger': '#8f0f1b', 'warning': '#5d3b00', 'info': '#064e62',
        'bg_body': '#d6d6d6', 'bg_card': '#e6e6e6', 'bg_alt': '#dedede',
        'text_primary': '#0a0a0a', 'text_secondary': '#262626',
        'text_muted': '#4d4d4d', 'on_accent': '#e6e6e6',
        'strong_weight': 0.70,
    },
    'ink': {
        'primary': '#252525', 'success': '#236b43',
        'danger': '#a33128', 'warning': '#805500', 'info': '#2a5f7e',
        'bg_body': '#d0d0cc', 'bg_card': '#deded9', 'bg_alt': '#d2d2cd',
        'text_primary': '#171719', 'text_secondary': '#3a3a3d',
        'text_muted': '#545459', 'on_accent': '#e8e8e4',
        'strong_weight': 0.70,
    },
    'slate': {
        'primary': '#285f8d', 'success': '#27654c',
        'danger': '#94433d', 'warning': '#735700', 'info': '#684d97',
        'bg_body': '#aebfd1', 'bg_card': '#c6d3e0', 'bg_alt': '#b4c4d4',
        'text_primary': '#14283a', 'text_secondary': '#304d66',
        'text_muted': '#354f66', 'on_accent': '#e5edf4',
        'strong_weight': 0.70,
    },
    'sepia': {
        'primary': '#8a4318', 'success': '#416b2b',
        'danger': '#9b3324', 'warning': '#705100', 'info': '#2e627e',
        'bg_body': '#d8be86', 'bg_card': '#e8d09a', 'bg_alt': '#ddbf83',
        'text_primary': '#35220c', 'text_secondary': '#60451f',
        'text_muted': '#674a24', 'on_accent': '#f0e2c6',
        'strong_weight': 0.70,
    },
}

# Maximum title length. A longer title is truncated (with an ellipsis)
# rather than rejected. The same cap is applied at the view layer so
# the value the admin sees in the input field matches what will
# actually land in the PDF.
_MAX_TITLE_LENGTH = 150

# Characters that are hostile on any filesystem. Stripped from the
# descriptive segment of the generated filename. Null and C0 control
# characters are also stripped.
_FILENAME_HOSTILE_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def _normalize_pdf_theme(raw):
    """Return a supported canonical theme name, defaulting safely."""
    value = str(raw or '').strip().lower()
    value = _PDF_THEME_ALIASES.get(value, value)
    return value if value in _PDF_THEME_TOKENS else _DEFAULT_PDF_THEME


def _normalize_pdf_locale(raw):
    value = str(raw or 'ar').strip().lower().split('-', 1)[0]
    return value if value in _PDF_COPY else 'ar'


def _mix_hex(foreground, background, weight):
    """Mix two six-digit hex colours; ``weight`` is the foreground share."""
    foreground = foreground.lstrip('#')
    background = background.lstrip('#')
    channels = []
    for index in (0, 2, 4):
        fg = int(foreground[index:index + 2], 16)
        bg = int(background[index:index + 2], 16)
        channels.append(round(fg * weight + bg * (1 - weight)))
    return '#' + ''.join(f'{channel:02x}' for channel in channels)


def _build_pdf_palette(theme):
    """Build document colours from the selected frontend theme tokens."""
    name = _normalize_pdf_theme(theme)
    palette = dict(_PDF_THEME_TOKENS[name])
    surface = palette['bg_card']
    palette.update({
        'name': name,
        'primary_dark': _mix_hex(
            palette['primary'], '#000000', palette['strong_weight'],
        ),
        'border': _mix_hex(palette['text_muted'], surface, 0.30),
        'soft_primary': _mix_hex(palette['primary'], surface, 0.14),
        'soft_success': _mix_hex(palette['success'], surface, 0.18),
        'soft_warning': _mix_hex(palette['warning'], surface, 0.18),
        'soft_danger': _mix_hex(palette['danger'], surface, 0.18),
        'soft_info': _mix_hex(palette['info'], surface, 0.14),
        'header_meta': _mix_hex(palette['on_accent'], palette['primary'], 0.78),
    })
    return palette


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


def _build_filters_summary(filters, locale='ar'):
    """
    Human-readable descriptions of the active filters, for the PDF
    header. Empty list when no filters are active.
    """
    if not filters:
        return []

    from ...models import Category, Tag

    copy = _PDF_COPY[_normalize_pdf_locale(locale)]
    parts = []

    search = (filters.get('search') or '').strip()
    if len(search) >= 2:
        parts.append(f'{copy["search"]}: «{search}»')

    raw_difficulties = _split_csv(filters.get('difficulty'))
    valid = [
        d.lower() for d in raw_difficulties
        if d.lower() in _VALID_DIFFICULTIES
    ]
    if valid:
        difficulty_labels = [copy['difficulty'].get(d, d) for d in valid]
        parts.append(f'{copy["difficulty_label"]}: {", ".join(difficulty_labels)}')

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
            parts.append(f'{copy["category"]}: {", ".join(names)}')

    raw_tag = (filters.get('tag') or '').strip()
    if raw_tag and Tag.objects.filter(name=raw_tag).exists():
        parts.append(f'{copy["tag"]}: {raw_tag}')

    tag_names = _split_csv(filters.get('tags_filter'))
    if tag_names:
        existing = list(
            Tag.objects
            .filter(name__in=tag_names)
            .order_by('name')
            .values_list('name', flat=True)
        )
        if existing:
            parts.append(f'{copy["tags"]}: {", ".join(existing)}')

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


def _resolve_doc_title(title, verified_only, locale='ar'):
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

    copy = _PDF_COPY[_normalize_pdf_locale(locale)]
    resolved = copy['default_title']
    if verified_only:
        resolved += f" {copy['verified_suffix']}"
    return resolved


def _normalize_front_matter(front_matter, locale):
    """Return bounded structured front matter, or None when disabled."""
    if not isinstance(front_matter, dict) or not front_matter.get('enabled'):
        return None

    copy = _PDF_COPY[locale]
    fields = []
    for item in (front_matter.get('fields') or [])[:10]:
        if not isinstance(item, dict):
            continue
        label = str(item.get('label') or '').strip()[:60]
        value = str(item.get('value') or '').strip()[:500]
        if label and value:
            fields.append({'label': label, 'value': value})

    return {
        'heading': (
            str(front_matter.get('heading') or '').strip()[:150]
            or copy['about_title']
        ),
        'body': str(front_matter.get('body') or '').strip()[:3000],
        'fields': fields,
    }


def _attach_pdf_image_uri(question):
    """Attach an in-memory data URI without exposing storage URLs."""
    question.pdf_image_uri = None
    if not getattr(question, 'image', None):
        return
    block = read_image_as_base64(question.image)
    if block and str(block.get('mime') or '').startswith('image/'):
        question.pdf_image_uri = (
            f"data:{block['mime']};base64,{block['data_base64']}"
        )


def export_questions_pdf(
    questions,
    *,
    verified_only=False,
    filters=None,
    title=None,
    theme=None,
    locale='ar',
    front_matter=None,
):
    """
    Render the given question list as a PDF.

    `questions` is a materialized list of Question objects, pre-loaded
    with the FKs and tags the template reads.

    `title` is optional. When supplied, it replaces the default header
    banner text and becomes the descriptive segment of the generated
    filename. When omitted, the default title is used.

    `theme` is the browser's active visual theme. Unknown values fall
    back to Stone rather than being interpolated into HTML or CSS.
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

    locale = _normalize_pdf_locale(locale)
    copy = _PDF_COPY[locale]

    # Attach presentation-only attributes. They are never persisted.
    for q in questions:
        q.difficulty_label = copy['difficulty'].get(
            q.difficulty, q.difficulty,
        )
        _attach_pdf_image_uri(q)

    font_path = resolve_arabic_font_path()
    font_uri = font_path.as_uri() if font_path else None
    if font_path is None:
        logger.warning(
            'Arabic TTF not found in FRONTEND_DIR/public/fonts or '
            'BASE_DIR/static/fonts — PDF will use a system fallback font.'
        )

    filters_summary = _build_filters_summary(filters, locale)
    category_color_css = _build_category_color_css(questions)
    doc_title = _resolve_doc_title(title, verified_only, locale)
    palette = _build_pdf_palette(theme)
    normalized_front_matter = _normalize_front_matter(front_matter, locale)

    html_string = render_to_string(
        'exports/questions_pdf.html',
        {
            'questions': questions,
            'verified_only': verified_only,
            'exported_at': timezone.now(),
            'filters_summary': filters_summary,
            'doc_title': doc_title,
            'pdf_theme': palette['name'],
            'locale': locale,
            'direction': copy['direction'],
            'labels': copy,
            'front_matter': normalized_front_matter,
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
    # interpolation slots are the font, selected palette, fallback category
    # colour, and generated per-category rules.
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
            background: {palette['bg_body']};

            @bottom-center {{
                content: counter(page) ' / ' counter(pages);
                font-size: 9pt;
                color: {palette['text_muted']};
                font-family: 'Noto Arabic', sans-serif;
            }}
            @bottom-right {{
                content: '{copy['brand']}';
                font-size: 8pt;
                color: {palette['text_muted']};
                font-family: 'Noto Arabic', sans-serif;
            }}
        }}

        html, body {{
            font-family: 'Noto Arabic', sans-serif;
            direction: {copy['direction']};
            text-align: {copy['text_align']};
            font-size: 10.5pt;
            line-height: 1.7;
            color: {palette['text_primary']};
            background: {palette['bg_body']};
            margin: 0;
            padding: 0;
        }}

        /* ── Optional front matter ───────────────────────────── */
        .about-page {{
            min-height: 22.8cm;
            box-sizing: border-box;
            display: flex;
            flex-direction: column;
            justify-content: center;
            padding: 1.4cm;
            background: {palette['bg_card']};
            border: 1px solid {palette['border']};
            border-block-start: 8px solid {palette['primary']};
            border-radius: 8px;
            break-after: page;
            page-break-after: always;
        }}

        .about-page h1 {{
            color: {palette['primary']};
            font-size: 24pt;
            line-height: 1.3;
            margin: 0 0 0.7em 0;
        }}

        .about-page__body {{
            color: {palette['text_secondary']};
            font-size: 12pt;
            line-height: 1.9;
            margin: 0 0 1.2em 0;
            white-space: pre-wrap;
        }}

        .about-page__fields {{
            margin: 0;
            padding: 0;
        }}

        .about-page__field {{
            display: flex;
            gap: 0.8em;
            padding: 0.55em 0;
            border-bottom: 1px solid {palette['border']};
        }}

        .about-page__field dt {{
            min-width: 28%;
            color: {palette['text_muted']};
            font-weight: 600;
        }}

        .about-page__field dd {{
            margin: 0;
            color: {palette['text_primary']};
            white-space: pre-wrap;
        }}

        /* ── Document header banner ──────────────────────────── */
        .doc-header {{
            margin: 0 0 1em 0;
            padding: 0.85em 1em;
            background: linear-gradient(135deg, {palette['primary']} 0%, {palette['primary_dark']} 100%);
            color: {palette['on_accent']};
            border-radius: 6px;
        }}

        .doc-header h1 {{
            font-size: 18pt;
            margin: 0 0 0.15em 0;
            font-weight: 700;
            color: {palette['on_accent']};
            letter-spacing: -0.01em;
            line-height: 1.35;
        }}

        .doc-meta {{
            margin: 0;
            font-size: 9pt;
            color: {palette['header_meta']};
        }}

        .doc-meta strong {{
            color: {palette['on_accent']};
            font-weight: 600;
        }}

        /* ── Filters summary strip ───────────────────────────── */
        .filters-applied {{
            font-size: 9pt;
            color: {palette['text_secondary']};
            background: {palette['bg_alt']};
            border-inline-start: 4px solid {palette['primary']};
            padding: 0.5em 0.8em;
            border-radius: 4px;
            margin: 0 0 1.2em 0;
            line-height: 1.6;
        }}

        .filters-applied strong {{
            color: {palette['primary']};
            font-weight: 700;
        }}

        /* ── Question card ───────────────────────────────────── */
        .question {{
            page-break-inside: avoid;
            background: {palette['bg_card']};
            border: 1px solid {palette['border']};
            border-inline-start: 4px solid {palette['border']};
            border-radius: 6px;
            padding: 0.85em 0.95em 0.75em 0.95em;
            margin-bottom: 0.85em;
        }}

        .question--easy   {{ border-inline-start-color: {palette['success']}; }}
        .question--medium {{ border-inline-start-color: {palette['warning']}; }}
        .question--hard   {{ border-inline-start-color: {palette['danger']}; }}

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
            background: {palette['text_primary']};
            color: {palette['bg_card']};
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
            background: {palette['soft_success']};
            color: {palette['success']};
        }}
        .q-difficulty--medium {{
            background: {palette['soft_warning']};
            color: {palette['warning']};
        }}
        .q-difficulty--hard {{
            background: {palette['soft_danger']};
            color: {palette['danger']};
        }}

        .q-category {{
            display: inline-flex;
            align-items: center;
            font-size: 9pt;
            color: {palette['text_muted']};
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
            border: 1px solid {palette['border']};
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
            color: {palette['text_primary']};
            line-height: 1.65;
            margin: 0 0 0.7em 0;
        }}

        .q-image {{
            display: block;
            max-width: 100%;
            max-height: 10cm;
            object-fit: contain;
            margin: 0.25em auto 0.75em auto;
            border: 1px solid {palette['border']};
            border-radius: 4px;
        }}

        /* ── Case stem panel ─────────────────────────────────── */
        .case-stem {{
            background: {palette['soft_info']};
            border-inline-start: 3px solid {palette['info']};
            padding: 0.55em 0.8em;
            margin: 0 0 0.65em 0;
            border-radius: 4px;
            font-size: 9.5pt;
            color: {palette['text_secondary']};
            line-height: 1.65;
        }}

        .case-stem-label {{
            display: block;
            font-weight: 700;
            color: {palette['info']};
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
            background: {palette['bg_alt']};
            border: 1px solid {palette['border']};
            font-size: 10pt;
            color: {palette['text_primary']};
            line-height: 1.55;
        }}

        .q-choice:last-child {{
            margin-bottom: 0;
        }}

        .q-choice--correct {{
            background: {palette['soft_success']};
            border-color: {palette['success']};
        }}

        .choice-marker {{
            display: inline-block;
            min-width: 20px;
            height: 20px;
            text-align: center;
            border-radius: 50%;
            background: {palette['text_primary']};
            color: {palette['bg_card']};
            font-size: 8.5pt;
            font-weight: 700;
            line-height: 20px;
            flex-shrink: 0;
            margin-inline-end: 8px;
            margin-top: 1px;
        }}

        .q-choice--correct .choice-marker {{
            background: {palette['success']};
            color: {palette['on_accent']};
        }}

        .choice-text {{
            flex: 1;
            min-width: 0;
        }}

        .choice-check {{
            color: {palette['success']};
            font-weight: 700;
            font-size: 11pt;
            flex-shrink: 0;
            margin-inline-start: 6px;
        }}

        /* ── Explanation ─────────────────────────────────────── */
        .q-explanation {{
            margin: 0.6em 0 0 0;
            padding: 0.6em 0.85em;
            background: {palette['soft_primary']};
            border-inline-start: 3px solid {palette['primary']};
            border-radius: 4px;
            font-size: 9.5pt;
            color: {palette['text_secondary']};
            line-height: 1.65;
        }}

        .q-explanation strong {{
            color: {palette['primary']};
            font-weight: 700;
        }}

        /* ── Footer / metadata ───────────────────────────────── */
        .q-footer {{
            margin-top: 0.6em;
            padding-top: 0.4em;
            border-top: 1px dashed {palette['border']};
            font-size: 8.5pt;
            color: {palette['text_muted']};
            line-height: 1.6;
        }}

        .q-meta-item {{
            display: inline;
            margin-inline-end: 14px;
        }}

        .q-meta-label {{
            color: {palette['text_secondary']};
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
            pdf_variant='pdf/a-3u',
            pdf_tags=True,
            srgb=True,
            custom_metadata=True,
            optimize_images=True,
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
