# backend/apps/users/services/permission_service.py
"""
Capability resolution for a User.

Resolution order
----------------
1. Unauthenticated or None    → empty set.
2. Role is 'admin'            → all capabilities.
3. Otherwise                  → role defaults from RoleCapabilities,
                                overlaid with per-user overrides.

The `User.capabilities` field is a dict of {capability_string: bool}.
A `True` value grants the capability even if the role does not hold it;
a `False` value revokes it even if the role does. Absent keys inherit
the role default. This is the standard three-state model
(inherit / force-on / force-off).

Caching
-------
Role defaults are cached under a per-role key for _CACHE_TTL seconds.
The cache is invalidated explicitly when the panel updates a role via
`invalidate_role_capabilities`. Per-user overrides are not cached here
because Django fetches the User instance fresh on every request, so
the override dict is always current.

A per-instance memo on the User object short-circuits repeated calls
to `user.has_capability()` inside a single request. The memo is
deliberately attached to the instance rather than to the cache, so it
dies with the request.
"""
from django.core.cache import cache

from ..capabilities import CAPABILITIES, DEFAULT_ROLE_CAPABILITIES


_CACHE_KEY = 'role_caps:{role}'
_CACHE_TTL = 300  # seconds


def role_capabilities(role):
    """
    Return the resolved capability set for a role.

    Reads from the cache first. On a miss, reads RoleCapabilities and
    intersects with CAPABILITIES so a stale DB row cannot grant a
    capability that no longer exists. Falls back to the seed constant
    when there is no DB row at all (e.g. a fresh deployment where
    `seed_capabilities` has not run yet).
    """
    if not role:
        return set()

    key = _CACHE_KEY.format(role=role)
    cached = cache.get(key)
    if cached is not None:
        return set(cached)

    # Lazy import to avoid a circular import at module load:
    # permission_service → models → permission_service.
    from ..models import RoleCapabilities

    row = RoleCapabilities.objects.filter(role=role).first()
    if row and isinstance(row.capabilities, list):
        caps = set(row.capabilities) & CAPABILITIES
    else:
        caps = set(DEFAULT_ROLE_CAPABILITIES.get(role, ()))

    cache.set(key, list(caps), _CACHE_TTL)
    return caps


def invalidate_role_capabilities(role=None):
    """
    Drop the cache for one role, or every role if `role` is None.

    Call this after any write to a RoleCapabilities row. The panel
    does this automatically; a manual DB edit will not, so if you edit
    rows directly, run `manage.py seed --only capabilities` (which invalidates
    all roles at the end) or restart the process.
    """
    if role:
        cache.delete(_CACHE_KEY.format(role=role))
        return
    for r in DEFAULT_ROLE_CAPABILITIES:
        cache.delete(_CACHE_KEY.format(role=r))


def resolve_for_user(user):
    """
    Return the full set of capabilities granted to `user`.

    The returned set is a fresh object; mutating it does not affect
    the cache or the user's stored overrides.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return set()

    # Admin bypass. Admins always hold the entire capability set
    # regardless of any role-default or override entry.
    if user.role == 'admin':
        return set(CAPABILITIES)

    caps = role_capabilities(user.role)

    overrides = getattr(user, 'capabilities', None) or {}
    if overrides:
        for cap, granted in overrides.items():
            # Silently drop overrides for capabilities that no longer
            # exist. A typo or a removed capability should not break
            # resolution for the rest of the set.
            if cap not in CAPABILITIES:
                continue
            if granted:
                caps.add(cap)
            else:
                caps.discard(cap)

    return caps


def resolved_capabilities_for_user(user):
    """
    Memoized variant used by User.resolved_capabilities().

    The memo lives on the User instance, which Django fetches fresh
    per request. This means:
      • Within a request, has_capability() is O(1) after the first call.
      • Across requests, the memo is discarded — never stale.

    If a code path mutates user.capabilities and re-checks within the
    same request, it must clear the memo. See UserCapabilitiesView.put
    for the pattern (delattr).
    """
    cached = getattr(user, '_resolved_caps_cache', None)
    if cached is not None:
        return cached
    caps = resolve_for_user(user)
    user._resolved_caps_cache = caps
    return caps