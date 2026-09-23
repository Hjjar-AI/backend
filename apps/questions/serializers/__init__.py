# backend/apps/questions/serializers/__init__.py
"""
Package surface for question serializers.

Every public name from the previous single-module
`apps.questions.serializers` is re-exported here, so external call
sites (`from apps.questions.serializers import QuestionSerializer`,
`from apps.questions.serializers import _resolve_case`, and every
variation used across the codebase) keep working unchanged after the
file was split into a package.

Module layout:

  • constants.py        — MAX_CHOICES and image-upload limits
  • case_resolver.py    — _resolve_case write-path helper
  • category.py         — CategorySerializer
  • tag.py              — TagSerializer, TagRenameSerializer, TagMergeSerializer
  • case.py             — CaseSummarySerializer, ClinicalCaseSerializer,
                          CaseStemUpdateSerializer
  • question_read.py    — QuestionSerializer
  • question_write.py   — QuestionCreateSerializer, QuestionUpdateSerializer
  • bulk.py             — QuestionBatchSerializer, BulkVerifySerializer,
                          BulkTagUpdateSerializer
  • image.py            — QuestionImageUploadSerializer
"""

from .constants import (
    MAX_CHOICES,
    MAX_IMAGE_SIZE,
    ALLOWED_IMAGE_EXTS,
    ALLOWED_IMAGE_MIMES,
)
from .case_resolver import _resolve_case
from .category import CategorySerializer
from .tag import (
    TagSerializer,
    TagRenameSerializer,
    TagMergeSerializer,
)
from .case import (
    CaseSummarySerializer,
    ClinicalCaseSerializer,
    CaseStemUpdateSerializer,
)
from .question_read import QuestionSerializer
from .knowledge_object import KnowledgeObjectSerializer
from .question_write import (
    QuestionCreateSerializer,
    QuestionUpdateSerializer,
)
from .bulk import (
    QuestionBatchSerializer,
    BulkVerifySerializer,
    BulkTagUpdateSerializer,
)
from .image import QuestionImageUploadSerializer


__all__ = [
    # Constants
    'MAX_CHOICES',
    'MAX_IMAGE_SIZE',
    'ALLOWED_IMAGE_EXTS',
    'ALLOWED_IMAGE_MIMES',
    # Case helper (public contract for master_exams + question_service)
    '_resolve_case',
    # Category
    'CategorySerializer',
    # Tag
    'TagSerializer',
    'TagRenameSerializer',
    'TagMergeSerializer',
    # Case
    'CaseSummarySerializer',
    'ClinicalCaseSerializer',
    'CaseStemUpdateSerializer',
    # Question read
    'QuestionSerializer',
    'KnowledgeObjectSerializer',
    # Question write
    'QuestionCreateSerializer',
    'QuestionUpdateSerializer',
    # Bulk
    'QuestionBatchSerializer',
    'BulkVerifySerializer',
    'BulkTagUpdateSerializer',
    # Image
    'QuestionImageUploadSerializer',
]
