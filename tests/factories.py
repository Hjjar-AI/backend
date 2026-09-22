# tests/factories.py
"""
Minimal factory helpers. Not a replacement for factory_boy — just
enough to keep test bodies short and to encode the two model-level
constraints test authors keep tripping over:

  • Question.published must have owned_by (CheckConstraint).
  • User.create_user requires a password and a valid username.

USERNAME VALIDATION
-------------------
The model's USERNAME_REGEX is `^[a-zA-Z0-9_\\u0600-\\u06FF]{3,50}$`.
That means: no hyphens, no dots, no spaces, at least 3 characters.
A test that calls make_user('flagged-author') or make_admin('the-admin')
fails inside create_user with an Arabic error message that is easy
to misread as an app bug. `_validate_username` catches that earlier
with an English message that names the factory.
"""
import re
from datetime import timedelta

from django.utils import timezone

from apps.users.models import User
from apps.questions.models import (
    Question, Category, Tag, ClinicalCase,
)


_counter = {'n': 0}
_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_\u0600-\u06FF]{3,50}$')


def _next(prefix):
    _counter['n'] += 1
    return f'{prefix}{_counter["n"]}'


def _validate_username(username):
    """
    Fail fast with a clear message when a test passes a username the
    model will reject. The regex is duplicated here on purpose — the
    model's own error message is in Arabic and does not name the
    factory, which makes the failure look like an app bug during a
    test run.
    """
    if not _USERNAME_RE.match(username):
        raise ValueError(
            f'tests.factories: username {username!r} is rejected by '
            f'USERNAME_REGEX. Rules: 3-50 chars, letters/digits/'
            f'underscore only, no hyphens or dots.'
        )


def make_user(username=None, password='test-pw-1234', **kw):
    if username is None:
        username = _next('user')
    _validate_username(username)
    return User.objects.create_user(username=username, password=password, **kw)


def make_admin(username=None, password='test-admin-pw-1234', **kw):
    if username is None:
        username = _next('admin')
    _validate_username(username)
    return User.objects.create_superuser(username=username, password=password, **kw)


def make_stub(username=None):
    if username is None:
        username = _next('stub')
    _validate_username(username)
    return User.objects.create_stub(username)


def make_category(name=None, **kw):
    if name is None:
        name = _next('cat-')
    return Category.objects.create(name=name, **kw)


def make_tag(name=None):
    if name is None:
        name = _next('tag-')
    return Tag.objects.create(name=name)


def make_case(key=None, **kw):
    if key is None:
        key = _next('case-')
    return ClinicalCase.objects.create(key=key, **kw)


def make_question(owner=None, **kw):
    """
    Build a Question.

    `owner` is used as both authored_by and owned_by unless overridden.
    The CheckConstraint on Question requires a published question
    (is_draft=False) to have owned_by set, so we always pass one.
    """
    if owner is None:
        owner = make_user()
    defaults = {
        'question': 'Sample question text?',
        'choices': ['A', 'B', 'C', 'D'],
        'correct_answer': 1,
        'explanation': 'Because.',
        'difficulty': 'medium',
        'authored_by': owner,
        'owned_by': owner,
        'is_draft': False,
    }
    defaults.update(kw)
    return Question.objects.create(**defaults)


def make_test_history(user, **kw):
    from apps.exams.models import TestHistory
    defaults = {
        'user': user,
        'mode': 'study',
        'total_questions': 10,
        'correct_count': 7,
        'accuracy': 70.0,
        'time_spent': 300,
        'completed_at': timezone.now(),
    }
    defaults.update(kw)
    return TestHistory.objects.create(**defaults)


def make_exam_session(user, question_ids=None, mode='study', **kw):
    from apps.exams.models import ExamSession
    import uuid
    defaults = {
        'session_id': str(uuid.uuid4()),
        'user': user,
        'mode': mode,
        'question_ids': question_ids or [],
        'answers': {},
        'current_index': 0,
        'is_active': True,
    }
    defaults.update(kw)
    return ExamSession.objects.create(**defaults)