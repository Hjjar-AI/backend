# backend/config/settings.py
from pathlib import Path
import os
from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Load .env file ─────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    _env_file = BASE_DIR / '.env'
    if _env_file.exists():
        load_dotenv(_env_file)
except ImportError:
    pass

DEBUG = os.environ.get('DEBUG', 'False') == 'True'

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-change-me'
    else:
        raise ImproperlyConfigured(
            'DJANGO_SECRET_KEY environment variable must be set when DEBUG=False'
        )

_INSECURE_SECRET_PLACEHOLDERS = {
    'your-secure-secret-key-here',
    'django-insecure-change-me',
}
if not DEBUG and SECRET_KEY in _INSECURE_SECRET_PLACEHOLDERS:
    raise ImproperlyConfigured(
        'DJANGO_SECRET_KEY is set to a known placeholder value. Generate a '
        'real secret before running with DEBUG=False: '
        'python -c "import secrets; print(secrets.token_hex(32))"'
    )

ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

if not DEBUG and '*' in ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        'ALLOWED_HOSTS must not contain "*" when DEBUG=False. '
        'Set it to an explicit comma-separated list of hostnames or IPs '
        'in backend/.env.'
    )

INSTALLED_APPS = [
    'jazzmin',

    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'rest_framework',
    'corsheaders',
    'django_filters',
    'sslserver',
    'apps.core',
    'apps.users',
    'apps.questions',
    'apps.learning',
    'apps.feedback',
    'apps.exams',
    'apps.master_exams',
    'apps.groups',
    'apps.planning',
    'apps.analytics',
    'apps.database',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    # LocaleMiddleware reads the `Accept-Language` request header and
    # activates the matching translation. It must run AFTER
    # SessionMiddleware (it can also read session-scoped language
    # preferences) and BEFORE CommonMiddleware (which is what hands
    # the activated locale to the rest of the request pipeline).
    #
    # The frontend sends `Accept-Language: <active locale>` on every
    # API request (see services/api/client.js), so this middleware is
    # what makes DRF's own error strings ("This field is required",
    # "Authentication credentials were not provided", etc.) render
    # in the user's chosen language instead of the hard-coded
    # default.
    #
    # NOTE: this translates messages that Django/DRF ship with. The
    # app's own api_error('...') calls still pass Arabic strings
    # directly; migrating those to gettext_lazy() is a separate,
    # much larger change.
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'apps.core.middleware.MustChangePasswordMiddleware',
    'apps.core.middleware.AutoRenewMiddleware',
    'apps.core.middleware.ActiveSessionMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# ── Database configuration ─────────────────────────────────────────────
#
# The active backend is MariaDB / MySQL. The previous SQLite block
# used to sit as a commented-out reference here; it has been removed
# because it duplicated the pattern already established by
# `apps/database/services/_sqlite_reference.py` — reference code for a
# disabled backend lives in a dedicated runnable module, not as dead
# comments inside the active configuration file.
#
# To run against SQLite, see the re-enable recipe in
# `apps/database/services/_sqlite_reference.py` and swap the block
# below for the commented alternative it documents.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.environ.get('DB_NAME', 'quiz'),
        'USER': os.environ.get('DB_USER', 'quiz'),
        'PASSWORD': os.environ.get('DB_PASSWORD', ''),
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '3306'),
        'OPTIONS': {
            'charset': 'utf8mb4',
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }
}

# ── Cache configuration ────────────────────────────────────────────────
#
# The active backend is selected by CACHE_TYPE (default: MemcachedCache).
# Memcached and Redis both work across multiple workers; the in-process
# LocMemCache does not, and is preserved below as a commented-out
# block rather than an implicit fallthrough.
#
# An earlier revision defaulted CACHE_TYPE to 'LocMemCache' and let any
# unrecognized value fall through to the LocMem branch. A typo in
# backend/.env — or the Flask-era value 'SimpleCache' — therefore
# silently landed every deployment on a per-process cache, which then
# produced per-worker throttle counters and per-worker login-IP
# buckets that looked correct until a second worker was added. The
# else branch below now fails closed so that misconfiguration surfaces
# at startup, not under load.
CACHE_TYPE = os.environ.get('CACHE_TYPE', 'MemcachedCache').strip()

if CACHE_TYPE == 'MemcachedCache':
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.memcached.PyMemcacheCache',
            'LOCATION': os.environ.get('MEMCACHED_LOCATION', '127.0.0.1:11211'),
        }
    }
elif CACHE_TYPE == 'RedisCache':
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': os.environ.get('REDIS_URL', 'redis://localhost:6379/1'),
            'OPTIONS': {'CLIENT_CLASS': 'django_redis.client.DefaultClient'},
        }
    }
else:
    raise ImproperlyConfigured(
        f'CACHE_TYPE={CACHE_TYPE!r} is not a recognized cache backend. '
        f'Valid values: MemcachedCache, RedisCache. '
        f'To use the in-process LocMemCache, follow the commented '
        f'block below instead of setting CACHE_TYPE.'
    )

# ── LocMemCache (commented alternative) ────────────────────────────────
#
# Per-process, single-worker only. Safe for local development and for
# a single-worker test run; unsafe behind any multi-worker deployment,
# because each worker has its own copy of the cache and DRF's throttle
# counters are stored there.
#
# To activate: comment out the entire if/elif/else block above and
# uncomment the CACHES assignment below.
#
# CACHES = {
#     'default': {
#         'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
#     }
# }

AUTH_PASSWORD_VALIDATORS = [
    # Kept: the one guard that still matters for account safety.
    # Min length 6 rather than the Django default of 8 so the
    # seeded pro-user passwords (firstname + "12345") pass.
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 6},
    },
    # Removed:
    #   • UserAttributeSimilarityValidator  — rejected passwords that
    #     shared a substring with the username. This was the primary
    #     source of false rejections on the seeded accounts (e.g.
    #     username "aya_kseibi", password "aya12345").
    #   • CommonPasswordValidator           — rejected the ~20 000
    #     most-used passwords. Too aggressive for a
    #     credential the admin hands out directly.
    #   • NumericPasswordValidator          — rejected all-numeric
    #     passwords. Kept the app's floor at "6 characters, any
    #     character class", which is what the seeded passwords use.
]
# ── Internationalization ───────────────────────────────────────────────
# LANGUAGE_CODE is the fallback when the request carries no
# Accept-Language header or names a language not in LANGUAGES.
LANGUAGE_CODE = 'ar'

LANGUAGES = [
    ('ar', _('Arabic')),
    ('en', _('English')),
]

LOCALE_PATHS = [BASE_DIR / 'locale']

USE_I18N = True
USE_TZ = True
TIME_ZONE = 'Asia/Damascus'

# ── Static files ───────────────────────────────────────────────────────
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'users.User'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    # NOTE: DEFAULT_PAGINATION_CLASS is configured for completeness
    # but is NOT reached by any view in this codebase.
    #
    # Every list endpoint goes through `apps.core.utils.paginate()`,
    # which parses `page` / `per_page` from the query string itself
    # and slices the queryset. That is because every list endpoint
    # is an `APIView` (not a DRF generic view), and DRF's
    # pagination is only auto-applied to `GenericAPIView`
    # subclasses via `paginate_queryset()`. No such subclass exists
    # here.
    #
    # The line is kept (rather than removed) because:
    #   • Removing it changes nothing at runtime.
    #   • Keeping it means a future view that DOES subclass a
    #     generic view inherits a sane default instead of
    #     `None` (which would silently return full querysets).
    #
    # If you want to migrate to DRF's built-in pagination, that is a
    # larger change: every view's call to `core.utils.paginate()`
    # would have to be replaced with `self.paginate_queryset(...)` +
    # `self.get_paginated_response(...)`, and the response envelope
    # shape (currently `{'items': [...], 'total': N, 'page': P,
    # 'per_page': PP, 'total_pages': TP}`) would change. Not a
    # drop-in replacement.
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'EXCEPTION_HANDLER': 'apps.core.exceptions.custom_exception_handler',
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/hour',
        'user': '1000/hour',
        # The `login` scope is unused: LoginView declares
        # LoginCredentialsRateThrottle and LoginIpRateThrottle, whose
        # scopes are `login_credentials` and `login_ip` below. Kept
        # here only as a placeholder if a future view wants a plain
        # per-scope login throttle.
        'login': '5/min',
        'login_credentials': '5/min',
        'login_ip': '30/min',
        'csrf': '30/hour',
        'import': '10/hour',
        'backup': '5/hour',
        'clear_db': '2/hour',
        'admin_password': '50/hour',
        'bulk_verify': '30/min',

        'master_exam_answer': '2000/hour',
        'master_exam_start': '10/hour',
    },
}

# Client-IP keying for throttling and login-attempt records uses
# REMOTE_ADDR exclusively:
#   • DRF's SimpleRateThrottle.get_ident() reads request.META['REMOTE_ADDR']
#     unless REST_FRAMEWORK['NUM_PROXIES'] is set (see apps/core/throttles.py
#     for every throttle class derived from SimpleRateThrottle).
#   • AuthenticationService.login_user and LoginSecurityService both read
#     request.META.get('REMOTE_ADDR', '') (see
#     apps/users/services/authentication_service.py and
#     apps/users/services/login_security_service.py).
#
# Consequence on a reverse-proxy deployment (nginx/Caddy → gunicorn):
# every client appears as the proxy IP, so:
#   1. LoginIpRateThrottle (30/min) becomes a single global ceiling for
#      the whole floor — one sprayer throttles every clinician. That is
#      a DoS, not privilege escalation.
#   2. LoginCredentialsRateThrottle (5/min per (IP, username)) collapses
#      to 5/min per username regardless of source IP. The exact
#      user-targeted DoS the (IP, user) keying was designed to prevent
#      — see the H-4 rationale in login_security_service.py — reappears.
#   3. LoginAttempt.ip is uniformly the proxy IP, so admin-side
#      forensics cannot distinguish clients behind the same proxy.
#
# Remediation path (do NOT apply until a reverse proxy is in place, and
# only after the proxy is configured to OVERWRITE — never append to —
# the inbound X-Forwarded-For):
#   • nginx: `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`
#     combined with an explicit `proxy_set_header X-Forwarded-For $remote_addr;`
#     at the top edge, or (cleanest) a `real_ip` block:
#         set_real_ip_from <trusted-proxy-cidr>;
#         real_ip_header X-Forwarded-For;
#         real_ip_recursive on;
#   • settings.py: set REST_FRAMEWORK['NUM_PROXIES'] to the exact count
#     of proxies between the app and the client (typically 1).
# A direct-exposed server must NOT set NUM_PROXIES — doing so lets any
# client spoof X-Forwarded-For and evade every IP-based control above.
# ───────────────────────────────────────────────────────────────────────

CORS_ALLOWED_ORIGINS = os.environ.get('CORS_ORIGINS', 'http://localhost:5173').split(',')
CORS_ALLOW_CREDENTIALS = True

# ── Security settings ─────────────────────────────────────────────────
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'

SESSION_COOKIE_AGE = 14 * 24 * 60 * 60        # 14 days — Django default, doctors

_default_secure = 'False' if DEBUG else 'True'
SESSION_COOKIE_SECURE = os.environ.get('USE_HTTPS', _default_secure) == 'True'

CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE

CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_HTTPONLY = True

CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'

MAX_UPLOAD_SIZE = int(os.environ.get('MAX_UPLOAD_SIZE', 50 * 1024 * 1024))

BACKUP_FOLDER = BASE_DIR / 'backups'
EXPORT_FOLDER = BASE_DIR / 'exports'
UPLOAD_FOLDER = BASE_DIR / 'uploads'

for folder in [BACKUP_FOLDER, EXPORT_FOLDER, UPLOAD_FOLDER]:
    folder.mkdir(parents=True, exist_ok=True)

MAX_QUIZ_QUESTIONS = 200
MAX_IMPORT_QUESTIONS = 5000
ITEMS_PER_PAGE = REST_FRAMEWORK['PAGE_SIZE']
MAX_CHOICES = 8

# Retention window for the PrivilegedAction audit log, in days.
# Swept by `manage.py cleanup_database` (aggregate housekeeping) and by
# `manage.py cleanup_privileged_actions` (standalone). Setting it to a
# larger value extends the audit trail; a smaller value reclaims disk
# on a high-traffic admin install. The task clamps the effective value
# to a minimum of 1 day so a misconfiguration cannot blank the table.
PRIVILEGED_ACTION_RETENTION_DAYS = int(
    os.environ.get('PRIVILEGED_ACTION_RETENTION_DAYS', 365)
)

MASTER_EXAM_MAX_QUESTIONS = 200              # per exam, matching MAX_QUIZ_QUESTIONS
MASTER_EXAM_GRACE_SECONDS = 180              # 3-minute grace after the timer hits 0
MASTER_EXAM_LAST_ANSWER_TOLERANCE_SECONDS = 2  # network jitter allowance (see attempt service)
MASTER_EXAM_AUTO_PUBLISH_DAYS = 30           # auto-publish-to-bank window
MASTER_EXAM_AUTO_PUBLISH_WARN_DAYS = (7, 1)  # in-app warnings before auto-publish

# ── Master-exam post-deadline window assertions ────────────────────────
#
# The two settings above define a single window on the SERVER side: an
# attempt is still accepted for `grace + tolerance` seconds after its
# `deadline_at`. On the CLIENT side, the runner's local grace timer
# (`attemptStore.inGraceWindow`, driven by `graceSeconds`) fires a
# `finish` request exactly `grace` seconds after the deadline. So the
# client's finish always fires `tolerance` seconds before the server's
# own window closes — that is the whole purpose of `tolerance`.
#
# Two invariants must hold for that race to stay safe:
#
#   1. `tolerance` must be positive. It is the buffer that lets an
#      in-flight answer submission still land after the client's
#      finish request has been dispatched. A zero (or negative)
#      value collapses the buffer and turns the race into a coin
#      flip. This is a hard fail.
#
#   2. The combined window must stay small enough that the client's
#      local grace countdown and the server's real acceptance window
#      do not diverge by enough to produce a visibly wrong UI (a
#      "time is up" overlay that lingers long after the server has
#      already stopped accepting). Ten minutes is generous for a
#      live exam; anything beyond that is almost certainly a typo.
#      This is a hard fail too — better to catch it at startup than
#      at exam time.
#
# A misconfigured deployment that raised `grace` to, say, 3600 would
# otherwise start cleanly and misbehave only when a live exam hit its
# deadline, which is the worst possible time to discover the mistake.
if MASTER_EXAM_LAST_ANSWER_TOLERANCE_SECONDS <= 0:
    raise ImproperlyConfigured(
        f'MASTER_EXAM_LAST_ANSWER_TOLERANCE_SECONDS must be positive '
        f'(got {MASTER_EXAM_LAST_ANSWER_TOLERANCE_SECONDS}). This value '
        f'is the buffer during which an in-flight answer submission '
        f'still lands after the client dispatches its finish request.'
    )

_MASTER_EXAM_POST_DEADLINE_WINDOW = (
    MASTER_EXAM_GRACE_SECONDS + MASTER_EXAM_LAST_ANSWER_TOLERANCE_SECONDS
)
if _MASTER_EXAM_POST_DEADLINE_WINDOW > 600:
    raise ImproperlyConfigured(
        f'MASTER_EXAM_GRACE_SECONDS ({MASTER_EXAM_GRACE_SECONDS}) + '
        f'MASTER_EXAM_LAST_ANSWER_TOLERANCE_SECONDS '
        f'({MASTER_EXAM_LAST_ANSWER_TOLERANCE_SECONDS}) = '
        f'{_MASTER_EXAM_POST_DEADLINE_WINDOW}s. The combined post-deadline '
        f'acceptance window must stay under 600s so the client-side '
        f'grace display and the server-side real acceptance window '
        f'cannot diverge for longer than a user would reasonably '
        f'find plausible.'
    )

JAZZMIN_SETTINGS = {
    "site_title": "Quiz Admin",
    "site_header": "Quiz",
    "site_brand": "Quiz",
    "welcome_sign": "Welcome to Quiz Admin",
    "copyright": "Quiz",
    "search_model": "users.User",
}

FRONTEND_DIR = BASE_DIR.parent / 'frontend'
FRONTEND_DIST = FRONTEND_DIR / 'dist'

# MariaDB/MySQL do not support partial (conditional) unique
# constraints. The one such constraint in the codebase is
# feedback.QuestionFlag.Meta.unique_open_flag_per_user_question;
# its enforcement is done in FeedbackService.flag_question on
# backends that cannot create the index. Django emits models.W036
# at check time on every manage.py invocation without this.
SILENCED_SYSTEM_CHECKS = ['models.W036']