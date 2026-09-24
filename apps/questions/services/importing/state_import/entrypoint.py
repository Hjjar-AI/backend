# backend/apps/questions/services/importing/state_import/entrypoint.py
"""
Public entry point for question-bank package imports (v2/v3 migrate to v4).

Three-phase flow used by the frontend:

    1. POST with analyze=true. No writes.
       → 200 { counts, unknown_authors: [...] }
    2. If unknown_authors is non-empty, the frontend opens the
       mapping modal and builds the `mapping` dict from the admin's
       choices.
    3. POST again with the same file, analyze=false (or omitted),
       and the mapping. This performs the write.

See ImportStateView in apps/database/views.py for the full contract.
"""
import json
import logging
from uuid import UUID

from django.conf import settings
from django.db import transaction

from ..author_resolution import (
    build_user_lookups,
    load_persisted_mappings,
    persist_mapping_decisions,
)
from ..validators import get_user_safe, verify_upload_mime
from ..staging import stage_upload, cleanup_staged_upload
from ...state_workbook import read_state_workbook, StateWorkbookError
from ...state_migrations import migrate_state_envelope, UnsupportedStateVersion
from .constants import (
    CONFLICT_KEEP_LOCAL,
    CONFLICT_USE_IMPORTED,
    STATE_IMPORT_MODE_MERGE,
    STATE_IMPORT_MODE_REPLACE,
    VALID_CONFLICT_STRATEGIES,
    VALID_STATE_IMPORT_MODES,
)
from .validation import _validate_state_envelope
from .preview import _analyze_unknown_authors, _preview_state
from .apply import _apply_state
from .conflicts import analyze_conflicts

logger = logging.getLogger(__name__)


def _build_completion_message(mode, counts):
    """
    Human-readable completion line for the import response.

    Merge and replace produce structurally different counts — merge
    never updates or deletes, replace never skips — so the message
    cannot be a single format string. Kept as a helper so the
    branch logic lives next to the counter keys it reads.
    """
    if mode == STATE_IMPORT_MODE_REPLACE:
        return (
            f"تم استيراد الحالة: "
            f"{counts.get('questions_created', 0)} سؤال جديد، "
            f"{counts.get('questions_updated', 0)} سؤال محدَّث، "
            f"{counts.get('questions_deleted', 0)} سؤال عام محذوف "
            f"(غير موجود في الحزمة)"
        )
    return (
        f"تم استيراد {counts.get('questions_created', 0)} سؤال "
        f"({counts.get('questions_skipped', 0)} موجود مسبقاً)"
    )


def import_state(
    file,
    username,
    mode=STATE_IMPORT_MODE_MERGE,
    dry_run=False,
    analyze=False,
    mapping=None,
    conflict_strategy=None,
    conflict_resolutions=None,
):
    """
    Import a versioned question-bank package.

    mode       'merge' (default) or 'replace'.
    dry_run    write nothing, return counts only.
    analyze    write nothing, return counts AND the list of unknown
               author names. Drives the frontend's mapping modal.
    mapping    per-call decisions for unknown author names, keyed by
               source username. Overrides any persisted
               ExternalAuthorMapping for this import AND is persisted
               at the end so the next import of the same source skips
               the prompt.

    TEMP-FILE NAME — see flat_import.import_file for the full note.
    Files are named `import_<uuid>.json` or `import_<uuid>.xlsx` so the periodic sweep in
    `apps/core/utils.py::cleanup_old_temp_files` can reclaim an
    upload that was orphaned mid-import.
    """
    if mode not in VALID_STATE_IMPORT_MODES:
        return {'error': 'وضع الاستيراد غير صالح', 'code': 400}
    if conflict_strategy is None:
        conflict_strategy = (
            CONFLICT_USE_IMPORTED
            if mode == STATE_IMPORT_MODE_REPLACE
            else CONFLICT_KEEP_LOCAL
        )
    if conflict_strategy not in VALID_CONFLICT_STRATEGIES:
        return {'error': 'استراتيجية التعارض غير صالحة', 'code': 400}
    if not isinstance(conflict_resolutions or {}, dict):
        return {'error': 'حلول التعارض غير صالحة', 'code': 400}
    if any(
        decision not in {CONFLICT_KEEP_LOCAL, CONFLICT_USE_IMPORTED}
        for decision in (conflict_resolutions or {}).values()
    ):
        return {'error': 'قرار تعارض غير صالح', 'code': 400}
    normalized_resolutions = {}
    for raw_uuid, decision in (conflict_resolutions or {}).items():
        try:
            uuid_str = str(UUID(str(raw_uuid).strip()))
        except (TypeError, ValueError, AttributeError):
            return {'error': 'معرّف سؤال التعارض غير صالح', 'code': 400}
        normalized_resolutions[uuid_str] = decision
    conflict_resolutions = normalized_resolutions

    filename = str(getattr(file, 'name', '')).lower()
    if filename.endswith('.json'):
        suffix = '.json'
    elif filename.endswith('.xlsx'):
        suffix = '.xlsx'
    else:
        return {'error': 'الرجاء اختيار ملف JSON أو XLSX كامل', 'code': 400}

    mime_error = verify_upload_mime(file)
    if mime_error is not None:
        return mime_error

    filepath = stage_upload(file, suffix)

    try:
        if suffix == '.xlsx':
            transfer_limit = min(
                settings.MAX_STATE_TRANSFER_SIZE,
                settings.MAX_UPLOAD_SIZE,
            )
            payload = read_state_workbook(
                filepath,
                max_uncompressed_size=transfer_limit * 4,
            )
        else:
            with open(filepath, 'r', encoding='utf-8') as f:
                payload = json.load(f)
        payload, migrations_applied = migrate_state_envelope(payload)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        StateWorkbookError,
        UnsupportedStateVersion,
    ):
        cleanup_staged_upload(filepath)
        return {'error': 'ملف الحالة غير صالح', 'code': 400}

    transfer_limit = min(
        settings.MAX_STATE_TRANSFER_SIZE,
        settings.MAX_UPLOAD_SIZE,
    )
    canonical_size = len(json.dumps(
        payload,
        ensure_ascii=False,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8'))
    if canonical_size > transfer_limit:
        cleanup_staged_upload(filepath)
        return {'error': 'حجم بيانات الحالة بعد فكها يتجاوز الحد', 'code': 413}

    try:
        err = _validate_state_envelope(payload)
        if err is not None:
            return err

        if (
            mode == STATE_IMPORT_MODE_REPLACE
            and payload.get('meta', {}).get('scope') != 'full'
        ):
            return {
                'error': (
                    'لا يمكن استخدام الاستبدال مع حزمة انتقائية. '
                    'استخدم الدمج/التحديث أو صدّر حزمة كاملة.'
                ),
                'code': 409,
            }

        acting_user = get_user_safe(username)
        if not acting_user:
            return {'error': 'المستخدم غير موجود', 'code': 404}

        uuid_to_user, name_to_user = build_user_lookups()
        persisted_mappings = load_persisted_mappings()

        if analyze:
            counts = _preview_state(
                payload,
                mode,
                conflict_strategy,
                conflict_resolutions,
            )
            unknown = _analyze_unknown_authors(
                payload,
                uuid_to_user,
                name_to_user,
                persisted_mappings,
            )
            conflicts = analyze_conflicts(payload)
            return {
                'message': 'معاينة الاستيراد',
                'mode': mode,
                'dry_run': True,
                'counts': counts,
                'unknown_authors': unknown,
                'conflicts': conflicts,
                'conflict_count': len(conflicts),
                'conflict_strategy': conflict_strategy,
                'migrations_applied': migrations_applied,
            }

        if dry_run:
            counts = _preview_state(
                payload,
                mode,
                conflict_strategy,
                conflict_resolutions,
            )
            return {
                'message': 'معاينة الاستيراد',
                'mode': mode,
                'dry_run': True,
                'counts': counts,
                'migrations_applied': migrations_applied,
            }

        # _apply_state and persist_mapping_decisions run inside ONE
        # outer transaction. A failure on either side rolls back
        # both — the previously separate transaction meant a partial
        # state where questions were committed but the admin's
        # mapping decisions were not, forcing a re-prompt on the
        # next import.
        with transaction.atomic():
            result = _apply_state(
                payload,
                username,
                acting_user,
                mode,
                uuid_to_user,
                name_to_user,
                persisted_mappings,
                mapping or {},
                conflict_strategy,
                conflict_resolutions or {},
            )
            persist_mapping_decisions(mapping or {}, acting_user)

        return {
            'message': _build_completion_message(mode, result),
            'mode': mode,
            'dry_run': False,
            'counts': result,
            'conflict_strategy': conflict_strategy,
            'migrations_applied': migrations_applied,
        }

    except ValueError as ve:
        msg = str(ve)
        if msg == 'REPLACE_BLOCKED_BY_MASTER_EXAMS':
            # The guard is scoped to PUBLIC orphans — public questions
            # that exist locally and are NOT covered by the envelope.
            # A live master exam that references a question the
            # envelope is merely updating does not trigger this. The
            # message names the actual constraint so an admin knows
            # whether deleting an exam or switching to merge mode is
            # the right next step.
            return {
                'error': (
                    'لا يمكن الاستبدال: توجد امتحانات رئيسية مرتبطة '
                    'بأسئلة عامة محلية غير موجودة في هذه الحزمة. '
                    'احذف الامتحانات المرتبطة أولاً، أو استخدم وضع الدمج.'
                ),
                'code': 409,
            }
        logger.warning('State import failed validation: %s', msg)
        return {'error': msg, 'code': 400}
    except Exception as e:
        logger.exception('State import failed: %s', e)
        return {'error': 'فشل استيراد الحالة', 'code': 500}
    finally:
        cleanup_staged_upload(filepath)
