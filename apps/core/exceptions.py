# backend/apps/core/exceptions.py

from rest_framework.exceptions import NotAuthenticated
from rest_framework.views import exception_handler


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)

    if response is not None:
        # ── Unauthenticated 403 → 401 upgrade ──────────────────────
        #
        # DRF can emit 403, not 401, for "no valid session":
        # `APIView.handle_exception` downgrades every
        #      `NotAuthenticated` to 403 when the first configured
        #      authentication class has no `WWW-Authenticate` header.
        #      `SessionAuthentication` returns None from
        #      `authenticate_header()`, so the downgrade always fires.
        #
        # The SPA needs to distinguish "session is gone, send the user
        # to /login" from "session is fine, capability is missing,
        # show an error". With both cases arriving as 403 the client
        # cannot act on the difference, so an expired session leaves
        # the user on a page that silently fails every request.
        #
        # Match the exception type rather than inferring authentication
        # failure from an anonymous request. In particular, CSRF
        # failures are PermissionDenied and must remain 403.
        if isinstance(exc, NotAuthenticated) and response.status_code == 403:
            response.status_code = 401

        # ── Envelope reshaping ────────────────────────────────────
        #
        # Unchanged from the previous version. Every DRF error —
        # whether it came from serializer validation, a permission
        # class, or an exception raised inside a view — is normalised
        # into the same `{code, message, details}` shape the SPA
        # reads.
        #
        # `response.status_code` is read AFTER the 403→401 upgrade
        # above, so the `code` field in the body matches the HTTP
        # status line. The previous version read it before any
        # modification, which is the same value in every case except
        # the new upgrade path.
        if isinstance(response.data, dict):
            if 'detail' in response.data:
                message = str(response.data['detail'])
                details = response.data
            else:
                message = 'طلب غير صحيح'
                details = {}
                for field, errors in response.data.items():
                    if isinstance(errors, list):
                        details[field] = '; '.join(str(e) for e in errors)
                    else:
                        details[field] = str(errors)
                if details:
                    first_key = next(iter(details))
                    message = details[first_key]
            response.data = {
                'code': response.status_code,
                'message': message,
                'details': details,
            }
        else:
            response.data = {
                'code': response.status_code,
                'message': str(response.data),
                'details': None,
            }

    return response
