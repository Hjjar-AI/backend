# tests/contracts/test_frontend_endpoints_resolve.py
"""
Contract test: every URL the frontend can emit must resolve to a real
Django view.

WHY THIS TEST EXISTS
--------------------
`frontend/tests/unit/endpoints.test.js` and
`frontend/tests/unit/services/endpoints-snapshot.test.js` verify the
frontend's URL registry is internally well-formed and stable across
edits. Neither of them knows anything about the backend's URLconf. A
backend route rename — or a hand-edited frontend path — passes both
of those tests and only surfaces at runtime as a 404 during a user
session.

This test closes that gap: it extracts every concrete URL the
frontend can emit, prepends the API prefix, and asserts each one
resolves to a real view rather than to one of the two catch-all
fallbacks registered at the bottom of `config/urls.py`.

WHY NOT JUST CALL resolve() AND CHECK FOR Resolver404
-----------------------------------------------------
`config/urls.py` appends two fallback patterns to `urlpatterns`:

    re_path(r'^api/.*$', api_not_found, name='api-404')
    re_path(r'^(?P<path>.*)$', spa_serve, name='spa')

Both are registered LAST so they only match a path that has already
fallen through every real route. The consequence for a naive
contract test is severe: `resolve('/api/v1/typo/')` does NOT raise
`Resolver404` — it returns a `ResolverMatch` whose `.func` is
`api_not_found`. A test that only checks "did resolve() raise?"
would therefore pass on every typo.

The check below imports `api_not_found` and `spa_serve` from
`config.urls` and asserts the resolved view is neither. That is the
whole contract.

WHAT THIS CATCHES
-----------------
  • A backend route renamed but not mirrored on the frontend, or
    vice versa.
  • A builder that interpolates a segment the backend does not
    accept at that position (e.g. an `int` route called with a
    non-numeric slug, or a nested path placed under the wrong
    prefix).
  • A typo in a hand-written path constant.
  • A whole namespace silently dropped from either side.

WHAT THIS DOES NOT CATCH
------------------------
  • Semantic drift. A route that still resolves but now means
    something different — an HTTP verb flip, a controller
    replacement — is invisible here.
  • Query-parameter contracts. Only the path component is checked.
  • Response-shape changes. Field-level drift is a serializer
    concern, not a URL-routing one, and is covered by the API
    tests under tests/api/.
  • Runtime auth. A path can resolve and still 403. This test
    answers "is this route registered", not "am I allowed to
    reach it". The capability-layer tests under tests/api/
    answer the second question.

WHEN YOU ADD A NEW ENDPOINT TO endpoints.js
-------------------------------------------
Nothing in this test needs updating. The extractor picks up the new
path automatically. If the path does not yet exist on the backend,
this test fails with the URL and a reason — add the backend route
first, then re-run.

WHEN YOU RENAME A BACKEND ROUTE
-------------------------------
Update `frontend/src/services/api/endpoints.js` in the same commit.
If you do not, this test fails in CI rather than at runtime in a
user session, which is the entire point.
"""

import re
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase
from django.urls import resolve
from django.urls.exceptions import Resolver404

from config.urls import api_not_found, spa_serve


# ── Paths and constants ───────────────────────────────────────────────

# Repo layout assumed: `backend/` and `frontend/` are siblings, with
# this suite under `backend/tests/`. `parents[3]` climbs from
# tests/contracts/<this file> to their shared project root.
FRONTEND_ENDPOINTS = (
    Path(__file__).resolve().parents[3]
    / 'frontend' / 'src' / 'services' / 'api' / 'endpoints.js'
)

# Mirrors API_BASE in endpoints.js. The axios client is created with
# `baseURL: API_BASE + '/'`, so every registry path is relative to
# this prefix at call time.
API_PREFIX = '/api/v1'

# Substitution for a `${...}` placeholder in a builder template.
# `1` matches every Django path converter except `<uuid:...>`, which
# no builder in the current registry uses. If a uuid converter is
# ever added, extend `DUMMY_SEGMENT` or introduce a per-builder
# substitution table here.
DUMMY_SEGMENT = '1'

# Sanity thresholds for the extractor. Both are intentionally far
# below the actual counts (the file has well over 100 paths and
# well over 30 builders today), so they only fire when the
# extractor has stopped matching entirely — not when the file
# legitimately shrinks by a few entries.
_MIN_EXPECTED_PATHS = 30
_MIN_EXPECTED_BUILDERS = 10


# ── Extractor regexes ─────────────────────────────────────────────────
#
# Two shapes are extracted from endpoints.js:
#
#   1. A plain quoted string whose value begins and ends with `/`.
#      The non-greedy `[^'"\s]*?` inner group is required so the
#      regex does not consume an unrelated quote later in the file;
#      it also correctly rejects `/api/v1` (no trailing slash),
#      which is the API_BASE fallback default and not an endpoint.
#
#   2. A backtick template literal beginning with `/`. Placeholders
#      inside `${...}` are concretized separately by
#      _PLACEHOLDER_RE below.
#
# Both regexes are applied AFTER comments are stripped, so an
# example path inside a `//` or `/* ... */` block is not mistaken
# for a live endpoint.

_STATIC_PATH_RE = re.compile(r"""['"](/[^'"\s]*?/)['"]""")
_TEMPLATE_PATH_RE = re.compile(r'`(/[^`]*)`')
_PLACEHOLDER_RE = re.compile(r'\$\{[^}]+\}')

_LINE_COMMENT_RE = re.compile(r'//[^\n]*')
_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)


# ── Extractor implementation ──────────────────────────────────────────


def _strip_comments(source: str) -> str:
    """
    Remove `//` line comments and `/* ... */` block comments.

    Order matters: block comments are removed first, so a `//`
    inside a block comment does not leave a trailing fragment
    behind.
    """
    source = _BLOCK_COMMENT_RE.sub('', source)
    source = _LINE_COMMENT_RE.sub('', source)
    return source


def _collect_frontend_paths(source: str):
    """
    Return (sorted_concrete_paths, builder_template_count).

    `sorted_concrete_paths` is a de-duplicated, sorted list of every
    concrete URL the frontend can emit, expressed relative to
    API_PREFIX — i.e. every entry starts with `/` but NOT with
    `/api/v1`.

    `builder_template_count` is the number of backtick templates the
    extractor found, regardless of whether their concretized form
    duplicates a static path. This is used as a second sanity signal
    so a change that silently drops every builder (e.g. hoisting
    them into a helper module) fails loudly instead of reducing
    coverage to statics-only.
    """
    source = _strip_comments(source)

    statics = set()
    for match in _STATIC_PATH_RE.finditer(source):
        path = match.group(1)
        # The API_BASE default is a bare `/api/v1` with no trailing
        # slash, so _STATIC_PATH_RE would not match it anyway. The
        # explicit skip is kept as a guard in case that constant is
        # ever rewritten with a trailing slash.
        if path.startswith(API_PREFIX):
            continue
        statics.add(path)

    builders = []
    for match in _TEMPLATE_PATH_RE.finditer(source):
        template = match.group(1)
        if template.startswith(API_PREFIX):
            continue
        concrete = _PLACEHOLDER_RE.sub(DUMMY_SEGMENT, template)
        builders.append(concrete)

    combined = statics | set(builders)
    return sorted(combined), len(builders)


# ── Test class ────────────────────────────────────────────────────────


@skipUnless(
    FRONTEND_ENDPOINTS.is_file(),
    (
        f'frontend/src/services/api/endpoints.js not found at '
        f'{FRONTEND_ENDPOINTS}. This test requires a full checkout '
        f'with backend/ and frontend/ as siblings; a backend-only '
        f'checkout should skip it rather than fail.'
    ),
)
class FrontendEndpointContractTests(SimpleTestCase):
    """
    SimpleTestCase — no database. `resolve()` reads the compiled
    URLconf, which is populated by `django.setup()` before any test
    runs.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = FRONTEND_ENDPOINTS.read_text(encoding='utf-8')
        cls.paths, cls.builder_count = _collect_frontend_paths(cls.source)

    # ── Extractor sanity guards ───────────────────────────────────

    def test_extractor_found_a_non_trivial_number_of_paths(self):
        """
        Guard test. If a future edit to endpoints.js changes the
        file format — for example hoisting every path into a shared
        constants module — the extractor regexes could silently stop
        matching and every path-related test below would pass by
        iterating over an empty list. This fires first.

        The threshold is intentionally far below the current count
        (the file has over 100 paths today) so it does not fire on a
        legitimate consolidation, only on a near-total extraction
        failure.
        """
        self.assertGreater(
            len(self.paths),
            _MIN_EXPECTED_PATHS,
            (
                f'Only {len(self.paths)} paths extracted from '
                f'{FRONTEND_ENDPOINTS.name}. Expected at least '
                f'{_MIN_EXPECTED_PATHS}. The extractor regexes in '
                f'this module may need updating to match a new file '
                f'format.'
            ),
        )

    def test_extractor_found_builder_templates(self):
        """
        Second guard, covering a failure mode the first one misses.
        If the format change is partial — the statics still match
        but the builders are hoisted or rewritten — the path count
        stays high enough to pass test_extractor_found_... above,
        but every builder URL is now untested. This test fails
        loudly on that shape.
        """
        self.assertGreater(
            self.builder_count,
            _MIN_EXPECTED_BUILDERS,
            (
                f'Only {self.builder_count} builder templates '
                f'extracted. Expected at least '
                f'{_MIN_EXPECTED_BUILDERS}. If builders are still '
                f'present in endpoints.js but not being matched, '
                f'the _TEMPLATE_PATH_RE regex has drifted.'
            ),
        )

    def test_no_duplicate_paths_in_extraction(self):
        """
        Sanity guard: the extractor de-duplicates via a set. This
        test asserts the de-duplication worked, so a builder whose
        concretized form happens to coincide with a static constant
        does not produce a spurious second entry in the failure
        list of the contract test below.
        """
        self.assertEqual(
            len(self.paths),
            len(set(self.paths)),
            'Extractor produced duplicate entries — the de-dup step '
            'in _collect_frontend_paths has regressed.',
        )

    # ── The contract ──────────────────────────────────────────────

    def _resolve_or_explain(self, full_path):
        """
        Return (ok, reason).

        `ok` is True when the path resolves to a real Django view.
        `reason` is None on success and a human-readable explanation
        on failure.

        The two fallbacks in config/urls.py are compared by object
        identity — `match.func is api_not_found` — rather than by
        name, so a future rename of either view does not silently
        disable the check.
        """
        try:
            match = resolve(full_path)
        except Resolver404:
            # In the current config/urls.py this branch is
            # unreachable: the SPA catch-all matches every path
            # starting with `/`. It is kept as a defensive guard so
            # that a future config that removes the catch-alls still
            # produces a useful failure message instead of a trace.
            return False, 'no URL pattern matched (Resolver404)'

        if match.func is api_not_found:
            return False, (
                'matched the /api/* 404 fallback — no real route is '
                'registered for this path'
            )
        if match.func is spa_serve:
            return False, (
                'matched the SPA catch-all — the path fell through '
                'every /api/ route'
            )
        return True, None

    def test_every_frontend_path_resolves_to_a_real_view(self):
        """
        The core contract. Each extracted path is prefixed with
        API_PREFIX (matching API_BASE in endpoints.js) and resolved
        against the live URLconf. A resolution that lands on either
        of the two fallback views is reported as a failure with the
        reason.

        The failure list is assembled in full before being asserted,
        so a developer fixing the first broken URL does not need to
        re-run the test to discover the second.
        """
        failures = []
        for rel_path in self.paths:
            full_path = API_PREFIX + rel_path
            ok, reason = self._resolve_or_explain(full_path)
            if not ok:
                failures.append(f'{rel_path}  ->  {reason}')

        self.assertEqual(
            failures,
            [],
            (
                '\n\nFrontend URLs that do not resolve on the '
                'backend:\n  '
                + '\n  '.join(failures)
                + '\n\nEither the backend route was renamed or '
                'removed, or the frontend registry in '
                'frontend/src/services/api/endpoints.js has a stale '
                'path. Fix both sides together and commit them as '
                'one change.\n'
            ),
        )
