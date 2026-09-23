# backend/config/urls.py

import mimetypes
from pathlib import Path

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.urls import include, path, re_path

from apps.exams.views import TestHistoryView
from apps.users.views.auth_views import AdminPasswordChangeView


def spa_serve(request, path=''):
    """
    Serve the built Vue SPA from frontend/dist/.
    """
    dist_root = Path(settings.FRONTEND_DIST).resolve()
    clean_path = (path or '').lstrip('/')
    candidate = (dist_root / clean_path).resolve() if clean_path else dist_root

    try:
        candidate.relative_to(dist_root)
    except ValueError:
        raise Http404('Invalid path')

    if candidate.is_file():
        content_type, _ = mimetypes.guess_type(str(candidate))
        return FileResponse(open(candidate, 'rb'), content_type=content_type)

    index_file = dist_root / 'index.html'
    if not index_file.is_file():
        return HttpResponse(
            'Frontend build not found. '
            'Run `pnpm build` inside the frontend/ directory, then reload.',
            status=503,
            content_type='text/plain; charset=utf-8',
        )
    return FileResponse(
        open(index_file, 'rb'),
        content_type='text/html; charset=utf-8',
    )


def api_not_found(request, path=''):
    """
    JSON 404 for /api/ paths that match no registered route.
    """
    return JsonResponse(
        {'code': 404, 'message': 'المسار غير موجود', 'details': None},
        status=404,
    )


urlpatterns = [
    # ── Admin password change override ────────────────────────────────
    #
    # MUST come before `path('admin/', admin.site.urls)`. Django's
    # admin registers `/admin/password_change/` on its own; this
    # override wins because URL resolution is first-match. The
    # wrapper clears `must_change_password` on success — without it,
    # MustChangePasswordMiddleware's admin-side redirect would loop
    # forever after a successful password change (Django admin does
    # not know about the custom flag).
    path(
        'admin/password_change/',
        AdminPasswordChangeView.as_view(),
        name='admin-password-change-override',
    ),

    # Admin (everything else)
    path('admin/', admin.site.urls),

    # ── API ───────────────────────────────────────────────────────────
    path('api/v1/', include('apps.core.urls')),
    path('api/v1/auth/', include('apps.users.urls')),

    path('api/v1/questions/', include('apps.questions.urls')),
    path('api/v1/questions/', include('apps.learning.urls')),
    path('api/v1/questions/', include('apps.feedback.urls')),
    # Master exam routes (must precede the general exam include)
    path('api/v1/exam/master/', include('apps.master_exams.urls')),

    path('api/v1/exam/', include('apps.exams.urls')),
    path('api/v1/study/', include('apps.exams.urls')),
    path('api/v1/recall/', include('apps.exams.urls')),

    path('api/v1/', include('apps.planning.urls')),
    path('api/v1/', include('apps.groups.urls')),
    path('api/v1/analytics/', include('apps.analytics.urls')),
    path('api/v1/database/', include('apps.database.urls')),
    path('api/v1/history/', TestHistoryView.as_view(), name='user-history'),
    path('api/v1/admin/history/', TestHistoryView.as_view(), name='admin-history'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

urlpatterns += [
    re_path(r'^api/.*$', api_not_found, name='api-404'),
    re_path(r'^(?P<path>.*)$', spa_serve, name='spa'),
]
