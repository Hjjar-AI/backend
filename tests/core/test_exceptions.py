# tests/core/test_exceptions.py
from django.test import SimpleTestCase
from rest_framework import status
from rest_framework.exceptions import (
    ValidationError, AuthenticationFailed, NotAuthenticated, NotFound,
    PermissionDenied,
)

from apps.core.exceptions import custom_exception_handler


class CustomExceptionHandlerTests(SimpleTestCase):
    def _handle(self, exc):
        return custom_exception_handler(exc, {})

    def test_authentication_failed_shape(self):
        response = self._handle(AuthenticationFailed('bad creds'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data['code'], 401)
        self.assertEqual(response.data['message'], 'bad creds')

    def test_not_found_shape(self):
        response = self._handle(NotFound('missing'))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['message'], 'missing')

    def test_permission_denied_shape(self):
        response = self._handle(PermissionDenied('nope'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['message'], 'nope')

    def test_session_authentication_downgrade_is_restored_to_401(self):
        exc = NotAuthenticated('login required')
        exc.status_code = 403
        response = self._handle(exc)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data['code'], 401)

    def test_validation_error_with_detail_key(self):
        exc = ValidationError({'detail': 'custom'})
        response = self._handle(exc)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['message'], 'custom')

    def test_validation_error_with_field_errors(self):
        exc = ValidationError({
            'question': ['This field is required.'],
            'choices': ['Must have at least two.'],
        })
        response = self._handle(exc)
        self.assertEqual(response.status_code, 400)
        # The handler surfaces the first field's message as `message`
        # and the full per-field map in `details`.
        self.assertIn(
            response.data['message'],
            ['This field is required.', 'Must have at least two.'],
        )
        self.assertEqual(
            response.data['details']['question'], 'This field is required.'
        )
        self.assertEqual(
            response.data['details']['choices'], 'Must have at least two.'
        )

    def test_non_dict_payload(self):
        """Some DRF exceptions serialize to a list; handler wraps it."""
        exc = ValidationError(['one', 'two'])
        response = self._handle(exc)
        self.assertEqual(response.status_code, 400)
        self.assertIn('code', response.data)
        self.assertIn('message', response.data)
