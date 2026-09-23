# backend/apps/questions/services/importing/author_resolution.py
"""
Author resolution for state-envelope imports.

Decides which local User (if any) an external author name maps to.
The resolution order is fixed and documented in `resolve_author`.

No dependencies on the sibling import modules — this file can be
imported from anywhere in the package.
"""

import logging

from ...models import ExternalAuthorMapping
from apps.users.models import User

logger = logging.getLogger(__name__)


def build_user_lookups():
    """
    One pass over the user table for both uuid and name lookup.

    Returns a (uuid_to_user, name_to_user) tuple. Both maps are built
    from the same .only() projection, so the whole table is read once
    regardless of how many authors the envelope references.

    Stub users are included — a stub created by a previous import of
    the same envelope must be found on the next one, or the importer
    would create a duplicate stub row.
    """
    uuid_to_user = {}
    name_to_user = {}
    for u in User.objects.only('id', 'uuid', 'username', 'is_stub'):
        uuid_to_user[str(u.uuid)] = u
        name_to_user[u.username] = u
    return uuid_to_user, name_to_user


def load_persisted_mappings():
    """
    Stored per-source-name decisions from ExternalAuthorMapping.

    Returns { source_username: { 'action': ..., 'user_id': ... } }.
    The dict shape matches what a per-call `mapping` payload looks
    like, so `resolve_author` can treat both uniformly.
    """
    return {
        m.source_username: {
            'action': m.action,
            'user_id': m.target_user_id,
        }
        for m in ExternalAuthorMapping.objects.all()
    }


def resolve_author(
    name,
    author_uuid,
    uuid_to_user,
    name_to_user,
    persisted_mappings,
    call_mappings,
    acting_user,
):
    """
    Returns (User | None, source_str).

    Sources:
      'local_uuid'  — matched a local user by uuid
      'local_name'  — matched a local user by name
      'mapping'     — a stored or per-call decision was applied
      'unknown'     — no local match, no decision
    """
    # 1. Local uuid — authoritative, cannot be overridden.
    if author_uuid and author_uuid in uuid_to_user:
        return uuid_to_user[author_uuid], 'local_uuid'

    # 2. Local name.
    if name and name in name_to_user:
        return name_to_user[name], 'local_name'

    # 3/4. Mapping decision — per-call overrides persisted.
    decision = None
    if name and call_mappings and name in call_mappings:
        decision = call_mappings[name]
    elif name and persisted_mappings and name in persisted_mappings:
        decision = persisted_mappings[name]

    if decision is not None:
        action = decision.get('action')
        if action == ExternalAuthorMapping.ACTION_USER:
            target_id = decision.get('user_id')
            target = User.objects.filter(id=target_id).first()
            if target is not None:
                return target, 'mapping'
        elif action == ExternalAuthorMapping.ACTION_STUB:
            stub = User.objects.create_stub(name)
            return stub, 'mapping'
        elif action == ExternalAuthorMapping.ACTION_NULL:
            return None, 'mapping'

    # 5. Unknown.
    return None, 'unknown'


def collect_authors_from_envelope(payload):
    """
    Return { name: { uuid, question_count, case_count } } for every
    author named on a question or clinical case in the envelope.
    Authors without a name are ignored.

    Used by the analyze pass to build the mapping-modal prompt. The
    count is shown to the admin ("Ali authored 12 questions") so a
    mapping decision is made with the right context.
    """
    authors = {}
    for entry in payload.get('questions') or []:
        name = (entry.get('authored_by_name') or '').strip()
        if not name:
            continue
        if name not in authors:
            authors[name] = {
                'uuid': entry.get('authored_by_uuid'),
                'question_count': 0,
                'case_count': 0,
            }
        authors[name]['question_count'] += 1

    for entry in payload.get('cases') or []:
        name = (entry.get('authored_by_name') or '').strip()
        if not name:
            continue
        if name not in authors:
            authors[name] = {
                'uuid': entry.get('authored_by_uuid'),
                'question_count': 0,
                'case_count': 0,
            }
        authors[name]['case_count'] += 1
    return authors


def persist_mapping_decisions(call_mappings, acting_user):
    """
    Write the caller's decisions to ExternalAuthorMapping so future
    imports of the same source apply them automatically.

    Called from inside the same outer transaction as `apply_state`
    in state_import.import_state, so a failure on either side rolls
    back both — the previously separate transaction meant a partial
    state where questions were committed but the admin's mapping
    decisions were not, forcing a re-prompt on the next import.
    """
    if not call_mappings:
        return
    for name, decision in call_mappings.items():
        action = decision.get('action')
        if action not in (
            ExternalAuthorMapping.ACTION_USER,
            ExternalAuthorMapping.ACTION_STUB,
            ExternalAuthorMapping.ACTION_NULL,
        ):
            continue
        target_id = (
            decision.get('user_id')
            if action == ExternalAuthorMapping.ACTION_USER
            else None
        )
        ExternalAuthorMapping.objects.update_or_create(
            source_username=name,
            defaults={
                'action': action,
                'target_user_id': target_id,
                'decided_by': acting_user,
            },
        )
