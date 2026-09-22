# backend/apps/core/throttles.py
from rest_framework.throttling import SimpleRateThrottle


# ═════════════════════════════════════════════════════════════════════
# Base classes
# ═════════════════════════════════════════════════════════════════════
#
# Six of the concrete throttles below were byte-identical except for
# `scope`. Three more were byte-identical except for `scope`. Both
# groups had the same key-construction strategy; only the cache scope
# differed. Hoisting the shared shape into two private base classes
# removes ~30 lines of duplication and makes a future key-strategy
# change (e.g. adding a proxy-header read) a single edit.
#
# Public class names are preserved verbatim — no caller changes.


class _IpScopedThrottle(SimpleRateThrottle):

    def get_cache_key(self, request, view):
        return self.cache_format % {
            'scope': self.scope,
            'ident': self.get_ident(request),
        }


class _UserScopedThrottle(SimpleRateThrottle):
    """
    Rate-limit keyed on the authenticated user id, falling back to the
    client IP when the caller is anonymous. Subclasses only need to
    set `scope`.
    """
    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            ident = request.user.pk
        else:
            ident = self.get_ident(request)
        return self.cache_format % {
            'scope': self.scope,
            'ident': ident,
        }


# ═════════════════════════════════════════════════════════════════════
# Concrete throttles
# ═════════════════════════════════════════════════════════════════════


# ── Login: two IP-keyed variants with unique key construction ───────
#
# These do NOT inherit from _IpScopedThrottle because their key format
# is genuinely different (one adds a username component; the other
# uses a `login-ip:` prefix). Both formats are load-bearing for the
# login-DoS story documented in settings.py — leave them as-is.

class LoginCredentialsRateThrottle(SimpleRateThrottle):
    """
    Per-(IP, username) key. Collapses to a per-username limit when a
    reverse proxy funnels every client through one IP — see the
    NUM_PROXIES comment in config/settings.py before deploying behind
    a proxy.
    """
    scope = 'login_credentials'

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        try:
            data = request.data if hasattr(request, 'data') else {}
            username = (data.get('username') if isinstance(data, dict) else '') or ''
        except Exception:
            username = ''
        username = str(username).strip().lower()
        return f'login-credentials:{ident}:{username}'


class LoginIpRateThrottle(SimpleRateThrottle):
    """Per-IP ceiling. Prefixed so it cannot collide with other scopes."""
    scope = 'login_ip'

    def get_cache_key(self, request, view):
        return f'login-ip:{self.get_ident(request)}'


# ── IP-keyed ────────────────────────────────────────────────────────

class CsrfRateThrottle(_IpScopedThrottle):
    scope = 'csrf'


class ImportRateThrottle(_IpScopedThrottle):
    scope = 'import'


class BackupRateThrottle(_IpScopedThrottle):
    scope = 'backup'


class ClearDatabaseRateThrottle(_IpScopedThrottle):
    scope = 'clear_db'


class BulkVerifyRateThrottle(_IpScopedThrottle):
    scope = 'bulk_verify'


# ── User-keyed ──────────────────────────────────────────────────────

class AdminPasswordRateThrottle(_UserScopedThrottle):
    scope = 'admin_password'


class MasterExamAnswerRateThrottle(_UserScopedThrottle):
    scope = 'master_exam_answer'


class MasterExamStartRateThrottle(_UserScopedThrottle):
    scope = 'master_exam_start'