# backend/apps/core/utils.py
import os
import time
from django.conf import settings
from django.middleware.csrf import get_token
from rest_framework.response import Response


def safe_int(raw, default, minimum=None, maximum=None):
    if raw is None or raw == '':
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def parse_csv_param(raw, *, coerce=None):
    """
    Parse a comma-separated query-string value into a list.

    Used by the multi-select filter shapes that appear on both the
    question list endpoint and the available-count endpoint. Before
    this helper existed, the same
    `raw.split(',') → strip → drop empties` sequence was hand-copied
    into four call sites in `questions/views/question_views.py`, and
    a fifth near-copy lived in the export path
    (`pdf_export._split_csv`).

    Parameters
    ----------
    raw       — the raw query-string value. May be None, '', a
                string, or (defensively) any iterable of items. A
                non-string iterable is treated as already-parsed and
                each item is stringified.
    coerce    — optional callable applied to each non-empty piece.
                If it raises `(TypeError, ValueError)`, the piece is
                dropped from the result. Typical use is `int` for
                id lists; omit for name/tag lists.

    Returns a list of the surviving items (strings, or whatever
    `coerce` returned). Empty input returns `[]`, so a caller never
    has to distinguish "no param" from "empty param".
    """
    if raw is None or raw == '':
        return []

    if isinstance(raw, str):
        pieces = raw.split(',')
    else:
        try:
            pieces = list(raw)
        except TypeError:
            return []

    result = []
    for piece in pieces:
        text = str(piece).strip()
        if not text:
            continue
        if coerce is not None:
            try:
                result.append(coerce(text))
            except (TypeError, ValueError):
                continue
        else:
            result.append(text)
    return result


def dedupe_ordered(items):
    """
    Return a copy of `items` with duplicates removed, order preserved.

    Replaces the hand-rolled `seen = set(); out = []; for …` idiom
    that had been copy-pasted into StartSessionView.post (question
    ids) and QuestionRatingsBatchView.get (rating-batch ids) with
    identical semantics.
    """
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def api_success(data=None, message="Success", code=200):
    return Response(
        {
            'code': code,
            'data': data,
            'message': message,
        },
        status=code,
    )


def api_error(message="Error", code=400, details=None):
    return Response(
        {
            'code': code,
            'message': message,
            'details': details,
        },
        status=code,
    )


def set_csrf_cookie(response, request):
    """
    Attach a session-aligned CSRF cookie to an API response.

    WHY THIS SETS `response.csrf_cookie_set = True`
    -----------------------------------------------
    `get_token(request)` sets `request.META['CSRF_COOKIE_NEEDS_UPDATE']
    = True`, which causes `django.middleware.csrf.CsrfViewMiddleware
    .process_response` to unconditionally call `_set_csrf_cookie()`
    after this view returns — using `settings.CSRF_COOKIE_AGE`
    (Django's default is one year) and `settings.CSRF_COOKIE_SECURE`.

    That unconditionally overwrote the `max_age` computed below
    (which is deliberately the session's remaining lifetime), so the
    session-aligned expiry this function exists to set was inert on
    every call. Django's `HttpResponseBase.set_cookie()` merges onto
    the existing cookie rather than replacing it: `if secure: ...`
    and `if httponly: ...` have no `else`, so those flags can only be
    *raised*, never cleared — the effective `Secure` was the logical
    OR of `request.is_secure()` and `settings.CSRF_COOKIE_SECURE`.

    Setting `response.csrf_cookie_set = True` short-circuits
    `CsrfViewMiddleware.process_response` at its first check, making
    this the sole writer of the cookie. The `secure=` value is read
    from `settings.CSRF_COOKIE_SECURE` (the single deliberate switch
    for that attribute, and already derived from
    `SESSION_COOKIE_SECURE` in `config/settings.py`) rather than
    `request.is_secure()`, so the two attributes cannot disagree.
    """
    token = get_token(request)

    try:
        max_age = request.session.get_expiry_age()
    except Exception:
        max_age = settings.SESSION_COOKIE_AGE

    response.set_cookie(
        settings.CSRF_COOKIE_NAME,
        token,
        max_age=max_age,
        secure=settings.CSRF_COOKIE_SECURE,
        httponly=settings.CSRF_COOKIE_HTTPONLY,
        samesite=settings.CSRF_COOKIE_SAMESITE,
        path='/',
    )
    response.csrf_cookie_set = True
    return response


def paginate(queryset, request, default_per_page=None):
    per_page_default = default_per_page or getattr(settings, 'ITEMS_PER_PAGE', 20)
    page = safe_int(request.query_params.get('page'), 1, minimum=1)
    per_page = safe_int(
        request.query_params.get('per_page'),
        per_page_default,
        minimum=1,
        maximum=500,
    )
    total = queryset.count()
    start = (page - 1) * per_page
    end = start + per_page
    meta = {
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page if total > 0 else 1,
    }
    return queryset[start:end], meta


def cleanup_old_temp_files(max_age_hours=1, safety_backup_max_age_hours=24 * 7):
    """
    Remove stale files from UPLOAD_FOLDER, EXPORT_FOLDER, and
    BACKUP_FOLDER, keyed on filename prefix and file mtime.

    PREFIX SET
    ----------
    Every generator of a temp file in this codebase is listed here.
    Adding a new one means adding its prefix; a generator that is
    not listed writes files that accumulate indefinitely, because
    the sweep silently skips anything whose prefix does not match.

      • 'import_'                     — flat / telegram / state uploads
                                        (the three importer entry points).
      • 'questions_export_'           — unverified flat export (xlsx/csv/json).
      • 'verified_questions_export_'  — verified flat export.
      • 'questions_state_'            — state envelope export, both the
                                        plain and the `_verified` suffix
                                        forms (they share the prefix).
      • 'pre_restore_'                — the pre-restore safety snapshot;
                                        kept on
                                        the longer window because it is
                                        the rollback artifact, not a
                                        temp file.

    Backups created by `BackupService.create_backup()`
    (`questions_backup_*.sql`) are deliberately NOT swept — they are
    user-managed artifacts, not temp files. They accumulate until an
    admin deletes them from the database admin page.
    """
    dirs = [settings.UPLOAD_FOLDER, settings.EXPORT_FOLDER, settings.BACKUP_FOLDER]
    now = time.time()
    temp_cutoff = now - max_age_hours * 3600
    safety_cutoff = now - safety_backup_max_age_hours * 3600
    prefixes_temp = [
        'import_',
        'questions_export_',
        'verified_questions_export_',
        'questions_state_',
    ]
    prefixes_safety = ['pre_restore_']
    deleted = 0
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for filename in os.listdir(d):
            if any(filename.startswith(p) for p in prefixes_safety):
                cutoff = safety_cutoff
            elif any(filename.startswith(p) for p in prefixes_temp):
                cutoff = temp_cutoff
            else:
                continue
            filepath = os.path.join(d, filename)
            try:
                if os.path.isfile(filepath) and os.path.getmtime(filepath) < cutoff:
                    os.unlink(filepath)
                    deleted += 1
            except OSError:
                pass
    return deleted