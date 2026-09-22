# backend/apps/database/views.py

from rest_framework.views import APIView
from django.http import FileResponse
from django.conf import settings
import json
import logging

from .services import BackupService
from .serializers import RestoreBackupSerializer, ClearDatabaseSerializer
from apps.core.permissions import HasCapability
from apps.core.utils import api_success, api_error
from apps.core.audit import log_privileged_action
from apps.core.reauth import admin_password_matches
from apps.core.throttles import (
    ImportRateThrottle,
    BackupRateThrottle,
    ClearDatabaseRateThrottle,
    AdminPasswordRateThrottle,
)
from apps.questions.services import ImportService, ExportService

logger = logging.getLogger(__name__)


def _import_file_or_error(request):
    file = request.FILES.get('file')
    if not file:
        return None, api_error('لم يتم اختيار ملف', 400)
    if file.size > settings.MAX_UPLOAD_SIZE:
        return None, api_error(
            f'حجم الملف يتجاوز الحد الأقصى '
            f'({settings.MAX_UPLOAD_SIZE // (1024 * 1024)} ميجابايت)',
            400,
        )
    return file, None


def _export_response(result, content_type='application/octet-stream'):
    if 'error' in result:
        return api_error(result['error'], result['code'])
    return FileResponse(
        open(result['filepath'], 'rb'),
        as_attachment=True,
        filename=result['filename'],
        content_type=content_type,
    )


# Every view in this module is gated by 'admin.database'. The
# capability is deliberately singular rather than split per operation:
# anyone who can clear the database can also read its size, and
# granting one without the other would be a footgun. If a future
# deployment needs read-only database info, split the capability and
# change required_capability on DatabaseInfoView only.


# ── Export filter extraction ──────────────────────────────────────
#
# The two export views below accept the same optional filter set the
# question list endpoint accepts. Every key is read verbatim from the
# query string; `flat_export._build_export_queryset` is the single
# place that validates and applies them.
#
# Only keys that are actually present in the query string are
# forwarded. A missing key is NOT sent as an empty string — the
# queryset builder already treats `None` and `''` as "no filter", so
# the two are equivalent, but omitting the key keeps the log line
# shorter and prevents a future reader from thinking the caller
# intended to filter on empty.
_EXPORT_FILTER_KEYS = (
    'search',
    'difficulty',
    'category_ids',
    'tag',
    'tags_filter',
)


def _extract_export_filters(request):
    """
    Return the filter dict for an export request, or None when no
    filter keys were provided at all.
    """
    filters = {}
    for key in _EXPORT_FILTER_KEYS:
        value = request.query_params.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if value:
            filters[key] = value
    return filters or None


def _extract_export_title(request):
    """
    Read the optional `title` query param.

    The title is NOT a filter — it does not narrow the queryset. It is
    a document label that PDF export uses for the header banner and
    the generated filename. The other three formats ignore it.

    Stripped and length-capped (150 chars, matching the cap in
    `pdf_export.export_questions_pdf`) so a runaway title in the query
    string cannot push the header banner across the whole first page.

    Returns None when the param is absent or empty. `ExportService`
    treats None as "use the default title".
    """
    raw = request.query_params.get('title')
    if not raw:
        return None
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    return cleaned[:150]


def _extract_export_theme(request):
    """Read the client-local theme used by PDF export."""
    raw = request.query_params.get('theme')
    if not raw:
        return None
    return str(raw).strip().lower()[:32] or None


class DatabaseInfoView(APIView):
    permission_classes = [HasCapability]
    required_capability = 'admin.database'

    def get(self, request):
        result = BackupService.get_info()
        if 'error' in result:
            return api_error(result['error'], result['code'])
        return api_success(data=result)


class CreateBackupView(APIView):
    permission_classes = [HasCapability]
    required_capability = 'admin.database'
    throttle_classes = [BackupRateThrottle]

    def post(self, request):
        result = BackupService.create_backup()
        if 'error' in result:
            return api_error(result['error'], result['code'])
        return api_success(data=result, code=201)


class ListBackupsView(APIView):
    """
    List backup files on disk.

    On a backend the service does not support, `list_backups()`
    returns `{'error': ..., 'code': 500}`. That shape is passed
    through `api_error` so the client sees the documented error
    envelope, matching every other view in this module.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.database'

    def get(self, request):
        result = BackupService.list_backups()
        if 'error' in result:
            return api_error(result['error'], result['code'])
        return api_success(data=result)


class RestoreBackupView(APIView):
    permission_classes = [HasCapability]
    required_capability = 'admin.database'
    throttle_classes = [BackupRateThrottle, AdminPasswordRateThrottle]

    def post(self, request):
        serializer = RestoreBackupSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error('بيانات الطلب غير مكتملة', 400, details=serializer.errors)

        if not admin_password_matches(request.user, serializer.validated_data['admin_password']):
            return api_error('كلمة مرور المدير غير صحيحة', 403)

        backup_name = serializer.validated_data['backup_name']
        result = BackupService.restore_backup(backup_name)
        if 'error' in result:
            return api_error(result['error'], result['code'])

        log_privileged_action(
            request,
            action='db.restore',
            target=None,
            target_repr=f'backup:{backup_name}',
            details={'backup_name': backup_name},
        )

        return api_success(data=result)


class ClearDatabaseView(APIView):
    permission_classes = [HasCapability]
    required_capability = 'admin.database'
    throttle_classes = [ClearDatabaseRateThrottle, AdminPasswordRateThrottle]

    def post(self, request):
        serializer = ClearDatabaseSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error(
                'كلمة مرور المدير الحالية مطلوبة', 400,
                details=serializer.errors,
            )

        if not admin_password_matches(request.user, serializer.validated_data['admin_password']):
            return api_error('كلمة مرور المدير غير صحيحة', 403)

        result = BackupService.clear_database()
        if 'error' in result:
            return api_error(result['error'], result['code'])

        log_privileged_action(
            request,
            action='db.clear',
            target=None,
            target_repr='database:all_content',
            details={'safety_backup_created': True},
        )

        return api_success(data=result)


class ImportDatabaseView(APIView):
    permission_classes = [HasCapability]
    required_capability = 'admin.database'
    throttle_classes = [ImportRateThrottle]

    def post(self, request):
        file, error = _import_file_or_error(request)
        if error is not None:
            return error

        result = ImportService.import_file(file, request.user.username)
        if 'error' in result:
            return api_error(result['error'], result['code'])
        return api_success(data=result, code=201)


class ImportTelegramView(APIView):
    permission_classes = [HasCapability]
    required_capability = 'admin.database'
    throttle_classes = [ImportRateThrottle]

    def post(self, request):
        file, error = _import_file_or_error(request)
        if error is not None:
            return error

        result = ImportService.import_telegram(file, request.user.username)
        if 'error' in result:
            return api_error(result['error'], result['code'])
        return api_success(data=result, code=201)


class ExportDatabaseView(APIView):
    """
    Flat export of every question (or every matching question, when
    filters are supplied in the query string).

    QUERY PARAMS
    ------------
    Filters (narrow the queryset — see flat_export._build_export_queryset):

        search=<text>              min length 2
        difficulty=easy,medium     comma-separated
        category_ids=<int>,<int>   positive ints
        tag=<name>                 single tag
        tags_filter=<name>,<name>  multi-tag (OR within the list)

    Document label (PDF only, ignored by Excel/CSV/JSON):

        title=<text>               max 150 chars; becomes the PDF
                                   header and the filename's
                                   descriptive segment

        theme=<name>               active client theme; validated by the
                                   PDF renderer, defaults to Stone

    When no params are supplied, the response is byte-identical to the
    pre-filter export.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.database'

    def get(self, request, fmt):
        filters = _extract_export_filters(request)
        title = _extract_export_title(request)
        theme = _extract_export_theme(request)
        result = ExportService.export_questions(
            fmt=fmt,
            verified_only=False,
            filters=filters,
            title=title,
            theme=theme,
        )
        return _export_response(result)


class ExportVerifiedDatabaseView(APIView):
    """
    Flat export of verified questions only. Accepts the same filter
    set and `title` param as ExportDatabaseView; the two are composed
    with AND.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.database'

    def get(self, request, fmt):
        filters = _extract_export_filters(request)
        title = _extract_export_title(request)
        theme = _extract_export_theme(request)
        result = ExportService.export_questions(
            fmt=fmt,
            verified_only=True,
            filters=filters,
            title=title,
            theme=theme,
        )
        return _export_response(result)


class ExportStateView(APIView):
    permission_classes = [HasCapability]
    required_capability = 'admin.database'

    def get(self, request):
        include_images = request.query_params.get('include_images', 'true') != 'false'
        verified_only = request.query_params.get('verified_only', 'false') == 'true'

        result = ExportService.export_state(
            include_images=include_images,
            verified_only=verified_only,
        )
        return _export_response(result, content_type='application/json')


class ImportStateView(APIView):
    """
    Import a state envelope. Requires 'admin.database'.

    Body (multipart):
        file            the .json envelope (required)
        mode            'merge' (default) | 'replace'
        dry_run         'true' | 'false' (default) — write nothing,
                        return counts only
        analyze         'true' | 'false' (default) — write nothing,
                        return counts AND the list of unknown author
                        names. This drives the frontend's mapping
                        modal: the modal is shown only when the
                        response carries a non-empty
                        `unknown_authors` list.
        mapping         JSON string, e.g.
                        {"Ali": {"action": "user", "user_id": 12},
                         "Sara": {"action": "stub"}}
                        Per-call decisions for unknown author names.
                        Overrides any persisted ExternalAuthorMapping
                        for this import AND is persisted at the end so
                        the next import of the same source skips the
                        prompt.
        admin_password  required when mode='replace' AND
                        dry_run=false AND analyze=false
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.database'
    throttle_classes = [ImportRateThrottle, AdminPasswordRateThrottle]

    def post(self, request):
        file, error = _import_file_or_error(request)
        if error is not None:
            return error

        mode = request.data.get('mode', 'merge')
        dry_run = request.data.get('dry_run', 'false') == 'true'
        analyze = request.data.get('analyze', 'false') == 'true'

        mapping = None
        mapping_raw = request.data.get('mapping')
        if mapping_raw:
            try:
                parsed = json.loads(mapping_raw)
            except (TypeError, ValueError):
                return api_error('صيغة mapping غير صالحة', 400)
            if not isinstance(parsed, dict):
                return api_error('mapping يجب أن يكون كائناً', 400)
            mapping = parsed

        is_write = not dry_run and not analyze
        if mode == 'replace' and is_write:
            admin_password = request.data.get('admin_password') or ''
            if not admin_password_matches(request.user, admin_password):
                return api_error('كلمة مرور المدير غير صحيحة', 403)

        result = ImportService.import_state(
            file,
            request.user.username,
            mode=mode,
            dry_run=dry_run,
            analyze=analyze,
            mapping=mapping,
        )
        if 'error' in result:
            return api_error(result['error'], result['code'])

        if is_write:
            log_privileged_action(
                request,
                action='db.import_state',
                target=None,
                target_repr='state:questions',
                details={
                    'mode': mode,
                    'counts': result.get('counts', {}),
                    'mapping_applied': bool(mapping),
                },
            )

        return api_success(
            data=result,
            code=200 if (dry_run or analyze) else 201,
        )
