# tests/core/test_utils.py
from django.contrib.sessions.backends.db import SessionStore
from django.test import SimpleTestCase, RequestFactory
from rest_framework.request import Request as DRFRequest

from apps.core.utils import (
    safe_int, api_success, api_error, paginate, set_csrf_cookie,
)
from tests.base import CacheClearingTestCase
from tests.factories import make_question, make_user


class SafeIntTests(SimpleTestCase):
    def test_none_uses_default(self):
        self.assertEqual(safe_int(None, 5), 5)

    def test_empty_string_uses_default(self):
        self.assertEqual(safe_int('', 5), 5)

    def test_valid_int_string(self):
        self.assertEqual(safe_int('10', 5), 10)

    def test_invalid_string_uses_default(self):
        self.assertEqual(safe_int('abc', 5), 5)

    def test_float_string_uses_default(self):
        self.assertEqual(safe_int('1.5', 5), 5)

    def test_minimum_clamp(self):
        self.assertEqual(safe_int('-5', 5, minimum=0), 0)

    def test_maximum_clamp(self):
        self.assertEqual(safe_int('100', 5, maximum=10), 10)

    def test_minimum_and_maximum_both_respected(self):
        self.assertEqual(safe_int('50', 5, minimum=0, maximum=10), 10)

    def test_value_in_range_passes_through(self):
        self.assertEqual(safe_int('7', 5, minimum=0, maximum=10), 7)

    def test_int_input_works(self):
        self.assertEqual(safe_int(42, 5), 42)


class ApiResponseShapeTests(SimpleTestCase):
    def test_api_success_shape(self):
        r = api_success(data={'x': 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['code'], 200)
        self.assertEqual(r.data['data'], {'x': 1})
        self.assertEqual(r.data['message'], 'Success')

    def test_api_success_custom_code(self):
        r = api_success(data={'x': 1}, code=201)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['code'], 201)

    def test_api_error_shape(self):
        r = api_error('bad input')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['code'], 400)
        self.assertEqual(r.data['message'], 'bad input')
        self.assertIsNone(r.data['details'])

    def test_api_error_with_details(self):
        r = api_error('bad', code=422, details={'field': ['required']})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.data['details'], {'field': ['required']})


class PaginateTests(CacheClearingTestCase):
    """
    `paginate()` reads `request.query_params`, which is a DRF-only
    attribute. This fixture builds the DRF Request explicitly rather
    than relying on `APIRequestFactory` to do it — some DRF versions
    have returned the underlying Django request from `.get()`, and
    the failure mode is a confusing `WSGIRequest has no attribute
    query_params` deep inside `paginate`.

    The `_req` helper below guarantees a DRF Request regardless of
    what the factory does.
    """
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.user = make_user('alice')
        for _ in range(25):
            make_question(owner=self.user)

    def _req(self, query=''):
        """
        Build a DRF Request from a Django request. The DRF Request
        wraps the Django one and exposes `.query_params`.
        """
        django_req = self.factory.get(f'/?{query}')
        return DRFRequest(django_req)

    def test_fixture_is_drf_request(self):
        """
        Guard test: if this ever fails, every other test in this
        class is silently wrong. Keeping the check inside the suite
        means a future refactor of the fixture cannot silently break
        the rest of the class without this test going red first.
        """
        req = self._req()
        self.assertTrue(
            hasattr(req, 'query_params'),
            f'Expected a DRF Request with .query_params, got '
            f'{type(req).__name__}',
        )

    def test_first_page_default_size(self):
        from apps.questions.models import Question
        qs = Question.objects.all().order_by('id')
        page, meta = paginate(qs, self._req())
        self.assertEqual(len(page), 20)
        self.assertEqual(meta['page'], 1)
        self.assertEqual(meta['per_page'], 20)
        self.assertEqual(meta['total'], 25)
        self.assertEqual(meta['total_pages'], 2)

    def test_second_page(self):
        from apps.questions.models import Question
        qs = Question.objects.all().order_by('id')
        page, meta = paginate(qs, self._req('page=2'))
        self.assertEqual(len(page), 5)
        self.assertEqual(meta['page'], 2)

    def test_per_page_override(self):
        from apps.questions.models import Question
        qs = Question.objects.all().order_by('id')
        page, meta = paginate(qs, self._req('per_page=10'))
        self.assertEqual(len(page), 10)
        self.assertEqual(meta['per_page'], 10)

    def test_per_page_clamped_to_max(self):
        from apps.questions.models import Question
        qs = Question.objects.all().order_by('id')
        page, meta = paginate(qs, self._req('per_page=99999'))
        self.assertEqual(meta['per_page'], 500)

    def test_page_below_one_clamps(self):
        from apps.questions.models import Question
        qs = Question.objects.all().order_by('id')
        page, meta = paginate(qs, self._req('page=0'))
        self.assertEqual(meta['page'], 1)

    def test_empty_queryset_total_pages(self):
        from apps.questions.models import Question
        qs = Question.objects.filter(id=999999)
        page, meta = paginate(qs, self._req())
        self.assertEqual(len(page), 0)
        self.assertEqual(meta['total'], 0)
        self.assertEqual(meta['total_pages'], 1)


class SetCsrfCookieTests(CacheClearingTestCase):
    """
    `set_csrf_cookie` reads `request.session`. A plain
    `RequestFactory` request does not carry one; we attach a
    SessionStore manually so the call has something to read.
    """
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()

    def _make_request(self):
        req = self.factory.get('/')
        req.session = SessionStore()
        req.session.save()
        return req

    def test_sets_cookie_and_flag(self):
        req = self._make_request()
        response = api_success(data={'ok': True})
        set_csrf_cookie(response, req)

        # The flag short-circuits Django's own CSRF middleware.
        self.assertTrue(getattr(response, 'csrf_cookie_set', False))
        # And a cookie was attached under the configured name.
        from django.conf import settings
        self.assertIn(settings.CSRF_COOKIE_NAME, response.cookies)