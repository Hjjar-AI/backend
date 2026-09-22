# backend/apps/questions/serializers/constants.py
"""
Serializer-level limits.

`MAX_CHOICES` is read from settings so a deployment can widen the
choice count without touching code; the other three are image-upload
limits that must stay in lockstep with the model's image field and
the export-side cap (MAX_EXPORT_IMAGE_BYTES in
apps/questions/services/exporting/image_export.py).

Keeping these here — rather than redefining them in every module that
needs one — means the write serializers and the image serializer
agree on the same numbers by construction.
"""

from django.conf import settings
from apps.questions.image_policy import (
    MAX_IMAGE_SIZE, ALLOWED_IMAGE_EXTS, ALLOWED_IMAGE_MIMES,
)


MAX_CHOICES = getattr(settings, 'MAX_CHOICES', 8)
