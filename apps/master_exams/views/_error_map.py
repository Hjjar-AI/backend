# backend/apps/master_exams/views/_error_map.py
"""
Shared translation table for master-exam service error codes.

Every mutating master-exam view used to inline its own
`except ValueError as e: msg = str(e); if msg == 'X': ...; if msg
== 'Y': ...` ladder — six byte-shape-identical copies across
`composition_views.py`, `crud_views.py`, and `lifecycle_views.py`,
plus `attempt_views.py`'s own `_attempt_error_details` table that
solved exactly the same problem for the attempt endpoints. A new
error code added in the service layer had to be threaded into six
call sites by hand.

This module owns the one mapping. Each view's `except ValueError`
block becomes three lines:

    except ValueError as e:
        msg, status, details = exam_error_response(str(e))
        return api_error(msg, status, details=details)

The raw code is forwarded in `details['code']` when it is a
recognised sentinel, so the frontend can branch on it without
parsing the localized message. When the string is not a recognised
code (an unexpected bare ValueError from somewhere deeper), the
message is passed through verbatim with a 400 and no `details.code`
— that preserves the previous behaviour for that case.

PREFIX MATCHING (fix — dynamic-suffix error codes)
--------------------------------------------------
Some service codes carry a runtime payload as a `:suffix` after the
sentinel name. The one today is `QUESTIONS_NOT_ACCESSIBLE`, raised
from `composition.add_questions` and `crud.create` as

    ValueError(f'QUESTIONS_NOT_ACCESSIBLE:{missing}')

where `missing` is a Python list of ids the caller cannot see. A
plain exact-match dict lookup misses that string, so a caller
would have fallen through to the "unrecognized, pass verbatim"
branch and received the raw code (with the id list) as the
response message instead of the localized Arabic string. The
`_PREFIX_ERROR_MESSAGES` table below handles that shape: it is
scanned only after the exact-match lookup fails.

Adding a new sentinel:
  1. Raise it from a service with a bare `ValueError('NEW_CODE')`.
  2. Add an entry to `_MASTER_EXAM_ERROR_MESSAGES` (exact match)
     or `_PREFIX_ERROR_MESSAGES` (prefix match).
  3. Nothing else changes — every view picks it up.
"""


# Exact-match codes: (message, HTTP status) per recognised sentinel.
# The status is 400 by default; 409 is used for optimistic-locking
# conflicts and reorder mismatches, where the correct client response
# is "reload and retry," not "fix the request."
#
# `EXAM_NOT_FOUND` is 404, not 400. It is raised by
# `lifecycle._locked_exam` when the row has disappeared between the
# caller's `get_object_or_404` and the service's row-lock acquisition
# — i.e. a concurrent delete. The correct client response is the
# same one a plain `get_object_or_404` miss would produce.
_MASTER_EXAM_ERROR_MESSAGES = {
    # ── Start-time gates ─────────────────────────────────────────
    'WINDOW_NOT_OPEN':          ('لم تفتح نافذة الامتحان بعد.', 400),
    'WINDOW_CLOSED':            ('انتهت نافذة الامتحان ولا يُسمح بامتحان بديل.', 400),
    'MAKEUP_ALREADY_USED':      ('لقد استخدمت امتحانك البديل بالفعل.', 400),
    'ALREADY_ATTEMPTED':        ('لقد أديت هذا الامتحان مسبقاً.', 400),
    'EXAM_NOT_STARTABLE':       ('هذا الامتحان غير متاح للبدء.', 400),

    # ── Attempt-flow ─────────────────────────────────────────────
    'TIME_EXPIRED':             ('انتهى وقت الامتحان وتم إنهاء المحاولة تلقائياً.', 400),
    'QUESTION_NOT_IN_ATTEMPT':  ('السؤال لا ينتمي إلى هذه المحاولة.', 400),
    'QUESTION_NOT_IN_EXAM':     ('السؤال ليس جزءاً من هذا الامتحان.', 400),
    'ATTEMPT_NOT_FOUND':        ('لا توجد محاولة.', 404),
    'ATTEMPT_ALREADY_COMPLETE': ('المحاولة منتهية.', 400),

    # ── Composition ──────────────────────────────────────────────
    'MODIFIED_BY_ANOTHER_USER': ('تم تعديل الامتحان من قبل شخص آخر — يرجى إعادة التحميل', 409),
    'REORDER_MISMATCH':         ('ترتيب الأسئلة لا يطابق القائمة الحالية', 409),
    'EXAM_WINDOW_STARTED':      ('لا يمكن تعديل الامتحان بعد فتح النافذة', 400),
    'INVALID_QUESTION_IDS':     ('معرفات أسئلة غير صالحة', 400),
    'INVALID_QUESTION_ID':      ('معرف السؤال غير صالح', 400),
    'MISSING_EXPECTED_VERSION': ('مطلوب رقم الإصدار للتعديل', 400),

    # ── Lifecycle ────────────────────────────────────────────────
    'EXAM_NOT_FOUND':           ('الامتحان غير موجود.', 404),
    'EXAM_ALREADY_PUBLISHED':   ('الامتحان منشور بالفعل', 400),
    'EMPTY_EXAM':               ('لا يمكن نشر امتحان بدون أسئلة', 400),
    'EXAM_TERMINAL':            ('لا يمكن تنفيذ هذا الإجراء على امتحان في هذه الحالة', 400),
    'WINDOW_NOT_CLOSED':        ('لا يمكن النشر في البنك قبل إغلاق نافذة الامتحان', 400),
    'ALREADY_PUBLISHED_TO_BANK': ('تم النشر في البنك مسبقاً', 400),
    'CANNOT_DELETE_PUBLISHED_TO_BANK': ('لا يمكن حذف امتحان تم نشره في البنك', 400),
    'INVALID_DELETE_MODE':      ('طريقة حذف غير صالحة', 400),
}


# Prefix-match codes: (prefix, message, HTTP status). Scanned only
# after the exact-match lookup above returns nothing. Each prefix
# entry covers a code whose message is parameterized by dynamic
# runtime data (e.g. the list of inaccessible question ids) that
# the client should NOT see in the response body.
#
# The details.code is deliberately None for prefix matches — the
# original code did not forward it either (the interpolated id list
# would have made the "code" value unstable and un-greppable).
_PREFIX_ERROR_MESSAGES = (
    ('QUESTIONS_NOT_ACCESSIBLE', 'بعض الأسئلة غير متاحة', 400),
)


def exam_error_response(code, default_status=400):
    """
    Map a service error string to a (message, status, details) tuple.

    Lookup order:

      1. Exact match in `_MASTER_EXAM_ERROR_MESSAGES` → localized
         message, mapped status, `details={'code': code}`.
      2. Prefix match in `_PREFIX_ERROR_MESSAGES` → localized
         message, mapped status, `details=None`.
      3. No match → the raw string is passed through verbatim with
         `default_status` (400 by default) and `details=None`,
         matching the previous behaviour of the inline ladders.
    """
    entry = _MASTER_EXAM_ERROR_MESSAGES.get(code)
    if entry is not None:
        message, status = entry
        return message, status, {'code': code}

    for prefix, message, status in _PREFIX_ERROR_MESSAGES:
        if code.startswith(prefix):
            return message, status, None

    return code, default_status, None