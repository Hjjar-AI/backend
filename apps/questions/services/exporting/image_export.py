# backend/apps/questions/services/exporting/image_export.py
"""
Read a Question's image FieldFile and produce the base64 block used
in the state envelope.

Uses Django's storage API (`.open()`, `.size`, `.name`) so a
non-local backend — S3, GCS, a CDN origin — works transparently.
"""

import base64
import logging
import mimetypes
from pathlib import Path
from apps.questions.image_policy import MAX_IMAGE_SIZE

logger = logging.getLogger(__name__)


# Per-image base64 size cap. A single image larger than this is
# skipped during export with a warning. Base64 inflates payloads by
# ~33%, so a 5 MB image becomes ~6.7 MB of JSON. Keeping the cap at
# the same value as MAX_IMAGE_SIZE (the upload-side limit) means
# any image the app accepted on upload fits in the export.
MAX_EXPORT_IMAGE_BYTES = MAX_IMAGE_SIZE


def read_image_as_base64(field_file):
    """
    Return { filename, mime, data_base64 } for a Question.image
    FieldFile, or None if the file is missing or too large.

    The returned filename is only the basename — the full storage
    path is not preserved on import, since `upload_to` regenerates
    a fresh path on the target side.

    Returns None (not an exception) for a missing file, an
    unreadable file, or a file over MAX_EXPORT_IMAGE_BYTES. The
    caller treats None as "no image for this question" and moves on.
    """
    if not field_file:
        return None
    try:
        size = field_file.size
    except (OSError, ValueError):
        return None
    if size > MAX_EXPORT_IMAGE_BYTES:
        logger.warning(
            'Skipping image %r on export: %d bytes exceeds cap %d',
            field_file.name, size, MAX_EXPORT_IMAGE_BYTES,
        )
        return None

    try:
        with field_file.open('rb') as fh:
            raw = fh.read()
    except OSError:
        logger.warning('Skipping image %r on export: read failed', field_file.name)
        return None

    mime, _ = mimetypes.guess_type(field_file.name)
    if not mime:
        mime = 'application/octet-stream'

    return {
        'filename': Path(field_file.name).name,
        'mime': mime,
        'data_base64': base64.b64encode(raw).decode('ascii'),
    }
