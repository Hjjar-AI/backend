# backend/apps/questions/services/importing/telegram_import.py
"""
Telegram poll-export import path.

Reads a JSON file exported from Telegram, extracts poll messages,
and persists them as questions. Correct answers that were not
explicitly chosen in the export are auto-guessed from vote counts,
with a localized warning written into the explanation field.
"""

import json
import logging

from django.conf import settings
from django.utils.translation import get_language

from .flat_import import _persist_records
from .staging import stage_upload, cleanup_staged_upload
from .validators import (
    MAX_CHOICES,
    MAX_JSON_IMPORT_ELEMENTS,
    get_user_safe,
    verify_upload_mime,
)

logger = logging.getLogger(__name__)


def _guessed_explanation():
    """
    Localized "the correct answer was auto-guessed" note for the
    Telegram importer.

    The frontend ships the same warning as an i18n string
    (`utility.telegramGuessedExplanation` in locales/{ar,en}/common.json)
    and displays it in the import preview so the user sees exactly
    what will be stored. The backend writes the note into the
    imported `Question.explanation` field. To keep the two sides
    consistent, this helper picks a string from the language that
    Django's LocaleMiddleware has already activated for the current
    request — the frontend sets `Accept-Language` on every API call
    (see services/api/client.js), so the same locale drives both the
    preview and the persisted value.

    If the backend ever gains real gettext catalogues (.po files in
    LOCALE_PATHS), replace this helper with a `gettext_lazy()` call
    and delete the two literals below. Until then, the two strings
    are the single source of truth for the backend side, and the
    frontend's i18n key is the single source of truth for the
    frontend side. They MUST be kept in sync.
    """
    if get_language() == 'en':
        return (
            "⚠️ The correct answer was auto-guessed from the poll "
            "results — please review."
        )
    return "⚠️ تم تخمين الإجابة الصحيحة آلياً من نتائج التصويت – يرجى مراجعتها."


def import_telegram(file, username):
    """
    Import a Telegram poll-export JSON file.

    Only messages carrying a `poll` dict with at least two non-empty
    answer texts become questions. Correct answers are taken from the
    `chosen` flag on the answer when present; otherwise the answer
    with the most voters is used and `_guessed_explanation()` is
    written into the explanation field.

    TEMP-FILE NAME — see flat_import.import_file for the full note.
    Files are named `import_<uuid>.json` so the periodic sweep in
    `apps/core/utils.py::cleanup_old_temp_files` can reclaim an
    upload that was orphaned mid-import.
    """
    if not file.name.lower().endswith('.json'):
        return {'error': 'الرجاء اختيار ملف JSON', 'code': 400}

    mime_error = verify_upload_mime(file)
    if mime_error is not None:
        return mime_error

    user = get_user_safe(username)
    if not user:
        return {'error': 'المستخدم غير موجود', 'code': 404}

    filepath = stage_upload(file, '.json')

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {'error': 'صيغة ملف تلغرام غير صالحة', 'code': 400}

        messages = data.get('messages', [])
        if not isinstance(messages, list):
            return {'error': 'صيغة ملف تلغرام غير صالحة', 'code': 400}

        if len(messages) > MAX_JSON_IMPORT_ELEMENTS:
            return {
                'error': f'ملف تلغرام كبير جداً. الحد الأقصى {MAX_JSON_IMPORT_ELEMENTS} رسالة.',
                'code': 400,
            }

        questions = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            poll = msg.get('poll')
            if not isinstance(poll, dict):
                continue

            raw_question = poll.get('question')
            if not isinstance(raw_question, str):
                continue
            q_text = raw_question.strip()
            if not q_text:
                continue

            raw_answers = poll.get('answers', [])
            if not isinstance(raw_answers, list):
                continue

            filtered_answers = []
            for ans in raw_answers:
                if not isinstance(ans, dict):
                    continue
                raw_text = ans.get('text')
                if not isinstance(raw_text, str):
                    continue
                text = raw_text.strip()
                if not text:
                    continue
                try:
                    voters = int(ans.get('voters', 0))
                except (TypeError, ValueError, OverflowError):
                    voters = 0
                filtered_answers.append({
                    'text': text,
                    'chosen': bool(ans.get('chosen', False)),
                    'voters': max(voters, 0),
                })

            if len(filtered_answers) < 2:
                continue
            if len(filtered_answers) > MAX_CHOICES:
                continue

            choices = [a['text'] for a in filtered_answers]
            correct_idx = 1
            chosen_idx = None
            for idx, a in enumerate(filtered_answers, 1):
                if a['chosen']:
                    correct_idx = idx
                    chosen_idx = idx
                    break

            if chosen_idx is None:
                max_votes = -1
                for idx, a in enumerate(filtered_answers, 1):
                    if a['voters'] > max_votes:
                        max_votes = a['voters']
                        correct_idx = idx

            message_text = msg.get('text')
            if not isinstance(message_text, str):
                message_text = ''
            explanation = _guessed_explanation() if chosen_idx is None else message_text

            questions.append({
                'question': q_text,
                'choices': choices,
                'correct_answer': correct_idx,
                'explanation': explanation,
                'tags': 'طب نفسي',
            })

        if not questions:
            return {'error': 'لم يتم العثور على أي أسئلة في الملف', 'code': 400}

        if len(questions) > settings.MAX_IMPORT_QUESTIONS:
            return {
                'error': (
                    f'عدد الأسئلة ({len(questions)}) يتجاوز '
                    f'الحد ({settings.MAX_IMPORT_QUESTIONS})'
                ),
                'code': 400,
            }

        count, skipped = _persist_records(
            questions, username, author=user, owner=user,
        )

        user.update_trust_score()
        return {
            'message': f'تم استيراد {count} سؤال من تلغرام',
            'imported': count,
            'skipped': skipped,
        }

    except ValueError as ve:
        return {'error': str(ve), 'code': 400}
    except Exception as e:
        logger.exception('Telegram import failed: %s', e)
        return {'error': 'فشل استيراد ملف تلغرام', 'code': 500}
    finally:
        cleanup_staged_upload(filepath)
