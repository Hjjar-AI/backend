# backend/apps/master_exams/serializers/__init__.py
"""
Package surface for the master-exam serializers.

`apps/master_exams/views/*` do `from ..serializers import (...)` with
many names. Every public serializer from the previous single-module
`apps.master_exams.serializers` is re-exported here, so those imports
keep working unchanged after the file was split into this package.

Module layout:

  • _constants.py       — MAX_CHOICES (shared)
  • acknowledgement.py  — MasterExamAcknowledgeSerializer,
                          MasterExamAcknowledgementSerializer
  • attempt.py          — MasterExamAttemptStartSerializer,
                          MasterExamAttemptSerializer,
                          MasterExamAttemptStatusSerializer,
                          MasterExamSubmitAnswerSerializer,
                          MasterExamGotoSerializer,
                          MasterExamFlagSerializer
  • composition.py      — MasterExamAddQuestionsSerializer,
                          MasterExamRemoveQuestionSerializer,
                          MasterExamReorderSerializer
  • draft.py            — MasterExamDraftSerializer,
                          MasterExamDraftCreateSerializer
  • exam_crud.py        — MasterExamListSerializer,
                          MasterExamDetailSerializer,
                          MasterExamCreateSerializer,
                          MasterExamUpdateSerializer,
                          MasterExamDeleteSerializer
"""

from ._constants import MAX_CHOICES
from .acknowledgement import (
    MasterExamAcknowledgeSerializer,
    MasterExamAcknowledgementSerializer,
)
from .attempt import (
    MasterExamAttemptStartSerializer,
    MasterExamAttemptSerializer,
    MasterExamAttemptStatusSerializer,
    MasterExamSubmitAnswerSerializer,
    MasterExamGotoSerializer,
    MasterExamFlagSerializer,
)
from .composition import (
    MasterExamAddQuestionsSerializer,
    MasterExamRemoveQuestionSerializer,
    MasterExamReorderSerializer,
)
from .draft import (
    MasterExamDraftSerializer,
    MasterExamDraftCreateSerializer,
)
from .exam_crud import (
    MasterExamListSerializer,
    MasterExamDetailSerializer,
    MasterExamCreateSerializer,
    MasterExamUpdateSerializer,
    MasterExamDeleteSerializer,
)


__all__ = [
    'MAX_CHOICES',
    # Acknowledgement
    'MasterExamAcknowledgeSerializer',
    'MasterExamAcknowledgementSerializer',
    # Attempt
    'MasterExamAttemptStartSerializer',
    'MasterExamAttemptSerializer',
    'MasterExamAttemptStatusSerializer',
    'MasterExamSubmitAnswerSerializer',
    'MasterExamGotoSerializer',
    'MasterExamFlagSerializer',
    # Composition
    'MasterExamAddQuestionsSerializer',
    'MasterExamRemoveQuestionSerializer',
    'MasterExamReorderSerializer',
    # Draft
    'MasterExamDraftSerializer',
    'MasterExamDraftCreateSerializer',
    # Exam CRUD
    'MasterExamListSerializer',
    'MasterExamDetailSerializer',
    'MasterExamCreateSerializer',
    'MasterExamUpdateSerializer',
    'MasterExamDeleteSerializer',
]