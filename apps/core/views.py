# backend/apps/core/views.py

import logging
from io import StringIO

from django.conf import settings as django_settings
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection, transaction
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.permissions import HasCapability
from apps.core.utils import api_error, api_success

from .models import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    Setting,
    Tip,
)
from .runtime_settings import DEFAULT_RUNTIME_SETTINGS

logger = logging.getLogger(__name__)


class HealthView(APIView):
    """
    Liveness probe. Unauthenticated by design.

    CHECKS DATABASE AND CACHE, NOT JUST THE PROCESS
    -----------------------------------------------
    The previous version returned `status: ok` unconditionally —
    which meant an uptime monitor pointed at this endpoint would
    report green even while the database was unreachable and every
    real API call was failing. The endpoint now performs two cheap
    probes:

      • `SELECT 1` against the default database connection.
      • A set/get round-trip against the configured cache backend.

    A failed probe flips the response status to 503 and reports
    which check failed. The HTTP status is what a monitor reacts to;
    the JSON body is what a human reads during an incident.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        checks = {'database': 'ok', 'cache': 'ok'}
        status_code = 200

        # ── Database ───────────────────────────────────────────────
        try:
            with connection.cursor() as cur:
                cur.execute('SELECT 1')
                cur.fetchone()
        except Exception as exc:
            logger.exception('Health check: database probe failed')
            checks['database'] = f'error: {type(exc).__name__}'
            status_code = 503

        # ── Cache ──────────────────────────────────────────────────
        # The key is unique enough that a concurrent request cannot
        # observe another caller's value. The TTL is short — 5 s —
        # so a stale value left behind by an unclean shutdown expires
        # well before the next monitor poll on any sane interval.
        probe_key = f'_health_probe_{timezone.now().timestamp()}'
        try:
            cache.set(probe_key, '1', 5)
            if cache.get(probe_key) != '1':
                raise RuntimeError('cache round-trip returned wrong value')
            cache.delete(probe_key)
        except Exception as exc:
            logger.exception('Health check: cache probe failed')
            checks['cache'] = f'error: {type(exc).__name__}'
            status_code = 503

        return api_success(
            data={
                'status': 'ok' if status_code == 200 else 'degraded',
                'checks': checks,
                'timestamp': timezone.now().isoformat(),
            },
            code=status_code,
        )


class PublicConfigView(APIView):
    """
    Runtime configuration for the frontend. Unauthenticated so the
    SPA can read limits before any session exists.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        return api_success(data={
            'max_quiz_questions': getattr(django_settings, 'MAX_QUIZ_QUESTIONS', 200),
            'max_choices': getattr(django_settings, 'MAX_CHOICES', 8),
            'items_per_page': getattr(django_settings, 'ITEMS_PER_PAGE', 20),
            'roles': {
                'admin': 'admin',
                'moderator': 'moderator',
                'member': 'member',
            },
        })


class TipsView(APIView):
    """
    Locale-aware tip list, falling back to the default locale when
    the requested locale has no active tips.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        requested = request.query_params.get('locale', DEFAULT_LOCALE)

        if requested not in SUPPORTED_LOCALES:
            requested = DEFAULT_LOCALE

        tips = list(
            Tip.objects
            .filter(locale=requested, is_active=True)
            .values_list('text', flat=True)
        )
        resolved = requested

        if not tips and requested != DEFAULT_LOCALE:
            tips = list(
                Tip.objects
                .filter(locale=DEFAULT_LOCALE, is_active=True)
                .values_list('text', flat=True)
            )
            resolved = DEFAULT_LOCALE

        return api_success(data={'tips': tips, 'locale': resolved})


class AdminSettingsView(APIView):
    """
    Read / write the three system-wide policy settings. 'backup'
    throttle scope because writes here are rare.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.settings'
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'backup'

    KNOWN_KEYS = tuple(DEFAULT_RUNTIME_SETTINGS)

    NUMERIC_KEYS = frozenset(DEFAULT_RUNTIME_SETTINGS)

    def get(self, request):
        stored = {
            row.key: row.value
            for row in Setting.objects.all()
        }
        payload = {
            key: stored.get(key, DEFAULT_RUNTIME_SETTINGS[key])
            for key in self.KNOWN_KEYS
        }
        return api_success(data=payload)

    def post(self, request):
        data = request.data
        if not isinstance(data, dict):
            return api_error('بيانات غير صالحة', 400)

        cleaned = {}
        for key in self.KNOWN_KEYS:
            if key not in data:
                continue

            raw = data[key]

            if key in self.NUMERIC_KEYS:
                try:
                    value = int(str(raw).strip())
                except (TypeError, ValueError):
                    return api_error(
                        f'القيمة المُدخلة لـ {key} غير صالحة. يجب أن تكون رقماً صحيحاً.',
                        400,
                    )
                if value < 0:
                    return api_error(
                        f'القيمة المُدخلة لـ {key} يجب أن تكون 0 أو أكثر.',
                        400,
                    )
                cleaned[key] = str(value)
            else:
                cleaned[key] = str(raw).strip()

        updated = []
        with transaction.atomic():
            for key, value in cleaned.items():
                Setting.objects.update_or_create(
                    key=key,
                    defaults={'value': value},
                )
                updated.append(key)

        return api_success(
            data={'updated': updated},
            message='تم تحديث الإعدادات',
        )


class SeedSampleQuestionsView(APIView):
    """
    Run the sample-question seeder from the admin UI. Returns the
    management command's stdout and stderr so the UI can display them.

    Delegates to the `seed` wrapper with `--only questions` so the
    sample-question seeder runs through the same orchestration path
    that bootstrap uses. The wrapper's `--only` selection executes
    only `seed_sample_questions` — no other seeder is triggered.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.seed'
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'backup'

    def post(self, request):
        stdout = StringIO()
        stderr = StringIO()

        try:
            call_command(
                'seed',
                only=['questions'],
                stdout=stdout,
                stderr=stderr,
            )
        except Exception as exc:
            logger.exception('seed --only questions failed')
            return api_error(
                'فشل تنفيذ الأمر',
                500,
                details={
                    'stdout': stdout.getvalue(),
                    'stderr': f'{type(exc).__name__}: {exc}',
                },
            )

        return api_success(data={
            'stdout': stdout.getvalue(),
            'stderr': stderr.getvalue() or None,
        })