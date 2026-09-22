# backend/apps/questions/services/importing/state_import/identity.py
"""
Identity resolution for ported entities.

Categories, Tags, and ClinicalCases each carry BOTH a portable uuid
and a human-meaningful key (name or key). The match order is:

  1. Local row with matching uuid   → the envelope's identity maps
                                       to a local row.
  2. Local row with matching key    → the SAME logical entity,
                                       regardless of uuid. LOCAL UUID
                                       WINS — we do not overwrite the
                                       local uuid with the envelope's,
                                       so other references from this
                                       installation remain coherent.
  3. No match                       → create with the envelope's uuid.

Case 2 is why this helper exists: a plain
`get_or_create(uuid=...)` raises IntegrityError when a same-name row
already exists under a different uuid.
"""


def _find_existing_by_uuid_or_key(model, uuid_str, key_field, key_value):
    """
    Return (instance_or_None, matched_by) where matched_by is one of
    'uuid', 'key', or 'none'.
    """
    existing = model.objects.filter(uuid=uuid_str).first()
    if existing is not None:
        return existing, 'uuid'
    existing = model.objects.filter(**{key_field: key_value}).first()
    if existing is not None:
        return existing, 'key'
    return None, 'none'