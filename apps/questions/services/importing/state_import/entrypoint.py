# backend/apps/questions/services/importing/state_import/entrypoint.py
"""
Public entry point for the state-envelope import path (format v2).

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

from django.db import transaction

from ..author_resolution import (
    build_user_lookups,
    load_persisted_mappings,
    persist_mapping_decisions,
)
from ..validators import get_user_safe, verify_upload_mime
from ..staging import stage_upload, cleanup_staged_upload
from .constants import (
    STATE_IMPORT_MODE_MERGE,
    STATE_IMPORT_MODE_REPLACE,
    VALID_STATE_IMPORT_MODES,
)
from .validation import _validate_state_envelope
from .preview import _analyze_unknown_authors, _preview_state
from .apply import _apply_state

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
):
    """
    Import a full state envelope (v2).

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
    Files are named `import_<uuid>.json` so the periodic sweep in
    `apps/core/utils.py::cleanup_old_temp_files` can reclaim an
    upload that was orphaned mid-import.
    """
    if mode not in VALID_STATE_IMPORT_MODES:
        return {'error': 'وضع الاستيراد غير صالح', 'code': 400}

    if not file.name.lower().endswith('.json'):
        return {'error': 'الرجاء اختيار ملف JSON', 'code': 400}

    mime_error = verify_upload_mime(file)
    if mime_error is not None:
        return mime_error

    filepath = stage_upload(file, '.json')

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    except Exception:
        cleanup_staged_upload(filepath)
        return {'error': 'ملف JSON غير صالح', 'code': 400}

    try:
        err = _validate_state_envelope(payload)
        if err is not None:
            return err

        acting_user = get_user_safe(username)
        if not acting_user:
            return {'error': 'المستخدم غير موجود', 'code': 404}

        uuid_to_user, name_to_user = build_user_lookups()
        persisted_mappings = load_persisted_mappings()

        if analyze:
            counts = _preview_state(payload, mode)
            unknown = _analyze_unknown_authors(
                payload,
                uuid_to_user,
                name_to_user,
                persisted_mappings,
            )
            return {
                'message': 'معاينة الاستيراد',
                'mode': mode,
                'dry_run': True,
                'counts': counts,
                'unknown_authors': unknown,
            }

        if dry_run:
            counts = _preview_state(payload, mode)
            return {
                'message': 'معاينة الاستيراد',
                'mode': mode,
                'dry_run': True,
                'counts': counts,
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
            )
            persist_mapping_decisions(mapping or {}, acting_user)

        return {
            'message': _build_completion_message(mode, result),
            'mode': mode,
            'dry_run': False,
            'counts': result,
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
