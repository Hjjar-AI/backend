# backend/apps/core/audit.py

import logging
from .models import PrivilegedAction

logger = logging.getLogger(__name__)


def log_privileged_action(request, action, target=None, target_repr=None, details=None):
    """
    Append one row to the audit log.

    ON WRITE FAILURE — WHY TWO LOG LINES
    ------------------------------------
    The `except` branch below calls BOTH `logger.exception(...)` and
    `logger.error('AUDIT_WRITE_FAILURE: ...')`. This is deliberate,
    not duplication:

      • `logger.exception(...)` writes the traceback with whatever
        logger configuration the deployment has for this module.
        It is the diagnostic signal — the shape of the failure, the
        stack, the exception type.

      • `logger.error('AUDIT_WRITE_FAILURE: ...')` writes a single
        greppable line with a stable marker. A monitoring job or an
        operator chasing "did we ever fail to write an audit row?"
        greps for `AUDIT_WRITE_FAILURE` — it cannot grep for a
        traceback format that varies by Python version and by the
        exception type. The marker is stable across every failure.

    Collapsing the two into one call would lose one of the two
    properties. If a deployment finds the double-emission noisy in
    log aggregation, the correct fix is to raise the `logger.error`
    line's level or route it to a dedicated channel — not to remove
    the marker. Removing the marker is what makes a future audit-gap
    investigation hard.

    The return value is None on failure (never raises), so a caller
    can do `log_privileged_action(...)` without a try/except even
    though the write may fail.
    """
    try:
        user = getattr(request, 'user', None) if request is not None else None
        is_authenticated = bool(user and getattr(user, 'is_authenticated', False))
        actor = user if is_authenticated else None
        actor_username = (
            getattr(user, 'username', None) if is_authenticated else None
        )

        if target is not None:
            target_type = type(target).__name__
            target_id = str(getattr(target, 'pk', target))
            if target_repr is None:
                target_repr = str(target)
        else:
            target_type = None
            target_id = None

        if target_repr is not None:
            target_repr = str(target_repr)[:255] or None
        if actor_username is not None:
            actor_username = str(actor_username)[:80] or None

        ip = None
        user_agent = None
        if request is not None:
            meta = getattr(request, 'META', {}) or {}
            ip = (meta.get('REMOTE_ADDR') or None)
            user_agent = (meta.get('HTTP_USER_AGENT') or '')[:255] or None
            if ip is not None:
                ip = str(ip)[:45] or None

        return PrivilegedAction.objects.create(
            actor=actor,
            actor_username=actor_username,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_repr=target_repr,
            ip=ip,
            user_agent=user_agent,
            details=details or {},
        )
    except Exception:
        # Two intentional log lines — see the function docstring.
        logger.exception(
            'Failed to write PrivilegedAction row (action=%s, actor=%s, target=%s)',
            action,
            getattr(getattr(request, 'user', None), 'username', None),
            target_repr,
        )
        logger.error(
            'AUDIT_WRITE_FAILURE: action=%s actor=%s target=%s',
            action,
            getattr(getattr(request, 'user', None), 'username', None),
            target_repr,
        )
        return None