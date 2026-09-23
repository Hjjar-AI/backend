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
        write_options = html.return_value.write_pdf.call_args.kwargs
        self.assertEqual(write_options['pdf_variant'], 'pdf/a-3u')
        self.assertTrue(write_options['pdf_tags'])
        self.assertTrue(write_options['srgb'])

    def test_locale_and_structured_front_matter_reach_template(self):
        question = SimpleNamespace(
            difficulty='hard', category_id=None, category=None,
        )
        front_matter = {
            'enabled': True,
            'heading': 'About the publisher',
            'body': 'A short introduction.',
            'fields': [
                {'label': 'Edition', 'value': '2026'},
                {'label': '', 'value': 'ignored'},
            ],
        }

        with TemporaryDirectory() as export_dir, override_settings(
            EXPORT_FOLDER=export_dir,
        ), patch(
            'apps.questions.services.exporting.pdf_export.render_to_string',
            return_value='<html></html>',
        ) as render, patch(
            'apps.questions.services.exporting.pdf_export.resolve_arabic_font_path',
            return_value=None,
        ), patch('weasyprint.CSS'), patch('weasyprint.HTML') as html:
            html.return_value.write_pdf.side_effect = (
                lambda path, **_: Path(path).write_bytes(b'%PDF-test')
            )
            result = export_questions_pdf(
                [question], locale='en', front_matter=front_matter,
            )

        self.assertNotIn('error', result)
        context = render.call_args.args[1]
        self.assertEqual(context['locale'], 'en')
        self.assertEqual(context['direction'], 'ltr')
        self.assertEqual(context['doc_title'], 'Question Bank')
        self.assertEqual(context['front_matter']['heading'], 'About the publisher')
        self.assertEqual(
            context['front_matter']['fields'],
            [{'label': 'Edition', 'value': '2026'}],
        )

    def test_question_image_is_embedded_as_a_data_uri(self):
        question = SimpleNamespace(
            difficulty='medium', category_id=None, category=None,
            image=object(),
        )

        with TemporaryDirectory() as export_dir, override_settings(
            EXPORT_FOLDER=export_dir,
        ), patch(
            'apps.questions.services.exporting.pdf_export.render_to_string',
            return_value='<html></html>',
        ), patch(
            'apps.questions.services.exporting.pdf_export.resolve_arabic_font_path',
            return_value=None,
        ), patch(
            'apps.questions.services.exporting.pdf_export.read_image_as_base64',
            return_value={
                'mime': 'image/png',
                'data_base64': 'cG5n',
                'filename': 'question.png',
            },
        ), patch('weasyprint.CSS'), patch('weasyprint.HTML') as html:
            html.return_value.write_pdf.side_effect = (
                lambda path, **_: Path(path).write_bytes(b'%PDF-test')
            )
            result = export_questions_pdf([question])

        self.assertNotIn('error', result)
        self.assertEqual(question.pdf_image_uri, 'data:image/png;base64,cG5n')


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
