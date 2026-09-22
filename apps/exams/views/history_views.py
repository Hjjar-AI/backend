# backend/apps/exams/views/history_views.py
"""
Completed-session history.

Mounted at two URLs in config/urls.py:
    /api/v1/history/         — caller's own history
    /api/v1/admin/history/   — any user's history, when the caller
                               holds 'tests.view_all_history'

CAPABILITY SEMANTICS
--------------------
The view accepts one of two capabilities:

  • 'tests.view_all_history' — the caller may read any user's
    history, or the whole table when no `user_id` filter is given.
  • 'tests.view_own_history' — the caller may read their OWN
    history only. A request with a `user_id` query parameter that
    names someone else still requires 'tests.view_all_history'.

The two capabilities are independent: holding 'view_all' does not
require 'view_own', and holding 'view_own' does not grant
'view_all'. A user with neither is refused with 403 regardless of
the query parameters. This is the first revision that actually
enforces the two strings — they were declared in
apps/users/capabilities.py and granted by default to every role,
but no code consulted them, so the panel switch did nothing.
"""

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from ..models import TestHistory
from ..serializers import TestHistorySerializer
from apps.core.utils import api_success, api_error, paginate


class TestHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        can_see_all = request.user.has_capability('tests.view_all_history')
        can_see_own = request.user.has_capability('tests.view_own_history')

        user_id_raw = request.query_params.get('user_id')
        queryset = TestHistory.objects.all()

        if user_id_raw:
            try:
                user_id = int(user_id_raw)
            except (TypeError, ValueError):
                return api_error('معرف المستخدم غير صالح', 400)

            if not can_see_all:
                # Viewing someone else's history requires view_all.
                if request.user.id != user_id:
                    return api_error('غير مصرح لك', 403)
                # Viewing own history requires view_own.
                if not can_see_own:
                    return api_error('غير مصرح لك', 403)

            queryset = queryset.filter(user_id=user_id)
        else:
            if not can_see_all:
                if not can_see_own:
                    return api_error('غير مصرح لك', 403)
                queryset = queryset.filter(user=request.user)

        page, meta = paginate(queryset, request)
        serializer = TestHistorySerializer(page, many=True)
        return api_success(data={'items': serializer.data, **meta})