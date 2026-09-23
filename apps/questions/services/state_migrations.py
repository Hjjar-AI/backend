"""In-memory migrations for older question-bank package envelopes."""

from .state_format import STATE_FORMAT, STATE_FORMAT_VERSION


class UnsupportedStateVersion(ValueError):
    pass


def _migrate_v2_to_v3(payload):
    meta = payload['meta']
    meta['scope'] = meta.get('scope') or 'full'
    meta['selection'] = meta.get('selection') or {}
    meta['version'] = 3
    meta['migrated_from_version'] = 2
    return payload


def _migrate_v3_to_v4(payload):
    payload.setdefault('knowledge_objects', [])
    payload['meta']['version'] = 4
    payload['meta']['migrated_from_version'] = 3
    return payload


PACKAGE_MIGRATIONS = {
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
}


def migrate_state_envelope(payload):
    """Upgrade a supported package to the current schema in place.

    Version 2 is the first deployed package schema. Version 3 adds package
    scope metadata plus optional question provenance and translations. Those
    fields remain absent while migrating old v2 questions so replace imports
    preserve local values instead of overwriting them with invented defaults.
    Version 4 adds knowledge objects and question revision dates.
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

    if version > STATE_FORMAT_VERSION:
        raise UnsupportedStateVersion(
            f'Unsupported package schema version: {version}'
        )

    applied = []
    while version < STATE_FORMAT_VERSION:
        migrate = PACKAGE_MIGRATIONS.get(version)
        if migrate is None:
            raise UnsupportedStateVersion(
                f'Unsupported package schema version: {version}'
            )
        previous = version
        payload = migrate(payload)
        meta = payload['meta']
        try:
            version = int(meta.get('version'))
        except (TypeError, ValueError):
            raise UnsupportedStateVersion(
                f'Migration from package schema {previous} produced '
                'an invalid version'
            ) from None
        if version <= previous:
            raise UnsupportedStateVersion(
                f'Migration from package schema {previous} did not advance'
            )
        applied.append(f'{previous}→{version}')

    meta.setdefault('scope', 'full')
    meta.setdefault('selection', {})
    return payload, applied
