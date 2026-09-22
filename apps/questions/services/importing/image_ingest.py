# backend/apps/questions/services/importing/image_ingest.py
"""
Persist a base64 image payload from a state envelope onto a Question.

Only the state-envelope importer carries images. The flat and Telegram
importers do not.
"""

import base64
import binascii
import logging

from django.core.files.base import ContentFile
from apps.questions.image_policy import MAX_IMAGE_SIZE

logger = logging.getLogger(__name__)


def decode_import_image(image_block):
    try:
        raw = base64.b64decode(image_block['data_base64'], validate=True)
    except (binascii.Error, KeyError, TypeError, ValueError):
        return None
    return raw if len(raw) <= MAX_IMAGE_SIZE else None


def apply_image(question, image_block):
    """
    Attach the base64 image in `image_block` to `question.image`.

    Returns True if the image was persisted, False if it was skipped
    (bad base64, missing key, storage failure). A skipped image is
    NOT a fatal error — the question row is already committed by the
    caller and the image is best-effort. The caller increments its
    `images_imported` counter only on True.
    """
    raw = decode_import_image(image_block)
    if raw is None:
        logger.warning('Skipping invalid or oversized image on question %s', question.id)
        return False

    filename = (image_block.get('filename') or f'q{question.id}.bin').strip()
    try:
        question.image.save(filename, ContentFile(raw), save=True)
    except Exception:
        logger.exception(
            'Failed to persist image for question %s', question.id,
        )
        return False

    return True
