from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.questions.services.exporting.pdf_export import (
    _build_pdf_palette,
    _normalize_pdf_theme,
    export_questions_pdf,
)
from apps.questions.services.exporting import ExportService
from tests.base import CacheClearingTestCase
from tests.factories import make_question, make_user


class PdfThemeTests(SimpleTestCase):
    def test_supported_theme_is_preserved(self):
        self.assertEqual(_normalize_pdf_theme('dark'), 'dark')
        self.assertEqual(_normalize_pdf_theme(' Blossom '), 'blossom')

    def test_light_alias_and_unknown_theme_fall_back_to_stone(self):
        self.assertEqual(_normalize_pdf_theme('light'), 'stone')
        self.assertEqual(_normalize_pdf_theme('not-a-theme'), 'stone')
        self.assertEqual(_normalize_pdf_theme(None), 'stone')

    def test_dark_and_stone_build_different_document_palettes(self):
        stone = _build_pdf_palette('stone')
        dark = _build_pdf_palette('dark')

        self.assertNotEqual(stone['bg_body'], dark['bg_body'])
        self.assertNotEqual(stone['bg_card'], dark['bg_card'])
        self.assertNotEqual(stone['text_primary'], dark['text_primary'])
        self.assertEqual(dark['primary'], '#e6a15b')

    def test_selected_theme_reaches_template_and_stylesheet(self):
        question = SimpleNamespace(
            difficulty='easy', category_id=None, category=None,
        )

        with TemporaryDirectory() as export_dir, override_settings(
            EXPORT_FOLDER=export_dir,
        ), patch(
            'apps.questions.services.exporting.pdf_export.render_to_string',
            return_value='<html></html>',
        ) as render, patch(
            'apps.questions.services.exporting.pdf_export.resolve_arabic_font_path',
            return_value=None,
        ), patch('weasyprint.CSS') as css, patch('weasyprint.HTML') as html:
            html.return_value.write_pdf.side_effect = (
                lambda path, **_: Path(path).write_bytes(b'%PDF-test')
            )

            result = export_questions_pdf([question], theme='dark')

        self.assertNotIn('error', result)
        self.assertEqual(render.call_args.args[1]['pdf_theme'], 'dark')
        stylesheet = css.call_args.kwargs['string']
        self.assertIn('background: #0d1218', stylesheet)
        self.assertIn('background: #18212a', stylesheet)
        self.assertIn('#e6a15b', stylesheet)


class PdfThemeRenderTests(CacheClearingTestCase):
    def test_real_pdf_output_changes_with_theme(self):
        owner = make_user('pdf_theme_owner')
        make_question(owner=owner, question='Themed PDF?')

        with TemporaryDirectory() as export_dir, override_settings(
            EXPORT_FOLDER=export_dir,
        ):
            stone = ExportService.export_questions(fmt='pdf', theme='stone')
            dark = ExportService.export_questions(fmt='pdf', theme='dark')

            stone_bytes = Path(stone['filepath']).read_bytes()
            dark_bytes = Path(dark['filepath']).read_bytes()

        self.assertTrue(stone_bytes.startswith(b'%PDF'))
        self.assertTrue(dark_bytes.startswith(b'%PDF'))
        self.assertNotEqual(stone_bytes, dark_bytes)
