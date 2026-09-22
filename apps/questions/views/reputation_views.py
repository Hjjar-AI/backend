# backend/apps/questions/views/reputation_views.py
"""
Admin-facing author-reputation maintenance endpoints.

MOVED FROM `apps.core.views`
----------------------------
`RefreshAuthorRanksView` used to live in `apps/core/views.py` and
deferred-import `apps.questions.services.AuthorReputationService`
inside its `post()` method. That was a cross-app reach from `core` (the
app every other app depends on) into `questions` (a feature app) — the
only place in the codebase where the dependency direction ran the
"wrong way" at the view layer.

The view's whole purpose is recomputing author reputation, which is a
`questions`-domain concern: `AuthorReputationService` lives in
`questions.services`, the trust-score fields live on the `User` model
but are only ever written by code in `questions`, and the capability
that gates this view (`admin.seed`) is also the capability that gates
the other question-bank admin operations (`SeedSampleQuestionsView`).
So `questions` is where it belongs.

WIRE CONTRACT — URL UNCHANGED
-----------------------------
The URL `/api/v1/admin/refresh-author-ranks/` is still registered from
`apps.core.urls`, which now imports this class directly. Keeping the
route in `core` was a deliberate choice: URLs are configuration, not
domain logic, and moving the route to `questions/urls.py` would have
changed the wire path to
`/api/v1/questions/admin/refresh-author-ranks/` — a breaking change
for any client that calls it. The architectural goal (get the view
code out of `core`) is achieved without a frontend coordination.
"""
# If you ever want to move the route too, grep for
# `refresh-author-ranks` under `frontend/` and update every hit — the
# exact number of call sites is not asserted here.
from rest_framework.views import APIView
from rest_framework.throttling import ScopedRateThrottle

from apps.core.permissions import HasCapability
from apps.core.utils import api_success


class RefreshAuthorRanksView(APIView):
    """
    Recompute User.trust_score and User.questions_count for every
    non-admin contributor. Idempotent.

    Requires 'admin.seed' — the same capability that gates the other
    question-bank seed/maintenance operations, on the theory that a
    deployment that wants to restrict who can run the sample-question
    seeder should also restrict who can trigger the reputation
    recompute.

    The `backup` throttle scope is inherited from the previous revision
    of this view in `core.views`; it is reused deliberately because the
    operation is rare and the scope already has a sane default rate.
    """
    permission_classes = [HasCapability]
    required_capability = 'admin.seed'
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'backup'

    def post(self, request):
        # Top-level import is now safe — this view lives in the same
        # app as the service it calls, so there is no cycle to defer.
        from ..services import AuthorReputationService

        result = AuthorReputationService.refresh_all()

        return api_success(data={
            'scanned': result.get('scanned', 0),
            'updated': result.get('updated', 0),
        })