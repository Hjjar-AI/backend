"""In-memory migrations for older question-bank package envelopes."""

from .state_format import STATE_FORMAT, STATE_FORMAT_VERSION


class UnsupportedStateVersion(ValueError):
    pass


def migrate_state_envelope(payload):
    """Upgrade a supported package to the current schema in place.

    Version 2 is the first deployed package schema. Version 3 adds package
    scope metadata plus optional question provenance and translations. Those
    fields remain absent while migrating old v2 questions so replace imports
    preserve local values instead of overwriting them with invented defaults.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get('meta'), dict):
        return payload, []
    meta = payload['meta']
    if meta.get('format') != STATE_FORMAT:
        return payload, []

    try:
        version = int(meta.get('version'))
    except (TypeError, ValueError):
        raise UnsupportedStateVersion('Invalid package schema version') from None

    applied = []
    if version == 2:
        meta['scope'] = meta.get('scope') or 'full'
        meta['selection'] = meta.get('selection') or {}
        meta['version'] = 3
        applied.append('2→3')
        version = 3

    if version != STATE_FORMAT_VERSION:
        raise UnsupportedStateVersion(
            f'Unsupported package schema version: {version}'
        )
    meta.setdefault('scope', 'full')
    meta.setdefault('selection', {})
    if applied:
        meta['migrated_from_version'] = 2
    return payload, applied
