"""Locations used by PDF export and deployment diagnostics."""

from pathlib import Path

from django.conf import settings

ARABIC_FONT_NAME = 'NotoSansArabic-VariableFont_wdth,wght.ttf'


def arabic_font_candidates():
    return (
        Path(settings.FRONTEND_DIR) / 'public' / 'fonts',
        Path(settings.BASE_DIR) / 'static' / 'fonts',
    )


def resolve_arabic_font_path():
    for directory in arabic_font_candidates():
        path = directory / ARABIC_FONT_NAME
        if path.is_file():
            return path
    return None
