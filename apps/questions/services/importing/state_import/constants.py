# backend/apps/questions/services/importing/state_import/constants.py
"""
Format identifiers and import-mode names for the state envelope.

v2 — emitted by this revision. Adds:
  • uuid on every ported entity (categories, tags, cases, questions)
  • authored_by_uuid / owned_by_uuid per question
  • meta.user_map: uuid → { username, full_name } for every user
    referenced by any question OR case in the envelope.

Only v2 is accepted by the importer.
"""

from apps.questions.services.state_format import STATE_FORMAT, STATE_FORMAT_VERSION

STATE_IMPORT_MODE_MERGE = 'merge'
STATE_IMPORT_MODE_REPLACE = 'replace'
VALID_STATE_IMPORT_MODES = {STATE_IMPORT_MODE_MERGE, STATE_IMPORT_MODE_REPLACE}
