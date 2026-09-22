# tests/questions/test_image_export.py
"""
read_image_as_base64 (export side) and apply_image (import side).

Both are used by the state envelope. The round-trip test uses
include_images=False, so neither has coverage anywhere else.

The export helper takes a Django FieldFile. Its interface is:
    .name  -> str
    .size  -> int (may raise OSError/ValueError)
    .open(mode) -> context manager yielding a file-like object

The tests below use a small local stub for that interface, so they
do not depend on real file storage being configured.

MIME LOOKUP IS SYSTEM-DEPENDENT
-------------------------------
`mimetypes.guess_type` reads the system mime database. On a Linux
host with an exhaustive /etc/mime.types, a name like 'weird.xyz'
returns 'chemical/x-xyz' — not None. Only an extension the system
database does not know at all returns None, and there is no
guaranteed-safe extension. The fallback path is therefore tested
with a filename that has NO extension, which
`mimetypes.guess_type` always returns (None, None) for.
"""
import base64

from apps.questions.services.exporting.image_export import (
    read_image_as_base64, MAX_EXPORT_IMAGE_BYTES,
)
from apps.questions.services.importing.image_ingest import apply_image
from tests.base import CacheClearingTestCase
from tests.factories import make_user, make_question


class _FakeFieldFile:
    """
    Minimal FieldFile stub for the export helper. The read context
    manager returns `payload`.
    """
    def __init__(self, name, payload, size=None, size_error=None):
        self.name = name
        self._payload = payload
        self._size = size if size is not None else len(payload)
        self._size_error = size_error

    @property
    def size(self):
        if self._size_error is not None:
            raise self._size_error
        return self._size

    def open(self, mode='rb'):
        return _ReadContext(self._payload)


class _ReadContext:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return _Reader(self._payload)

    def __exit__(self, *exc):
        return False


class _Reader:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload


class ReadImageAsBase64Tests(CacheClearingTestCase):
    def test_none_field_returns_none(self):
        self.assertIsNone(read_image_as_base64(None))

    def test_happy_path_returns_dict(self):
        payload = b'\x89PNG\r\n\x1a\nfake-png-bytes'
        ff = _FakeFieldFile('upload.png', payload)
        result = read_image_as_base64(ff)
        self.assertIsNotNone(result)
        self.assertEqual(result['filename'], 'upload.png')
        self.assertEqual(result['mime'], 'image/png')
        decoded = base64.b64decode(result['data_base64'])
        self.assertEqual(decoded, payload)

    def test_oversized_image_skipped(self):
        payload = b'x' * 100
        ff = _FakeFieldFile(
            'big.png', payload, size=MAX_EXPORT_IMAGE_BYTES + 1,
        )
        self.assertIsNone(read_image_as_base64(ff))

    def test_at_cap_is_included(self):
        payload = b'x' * 100
        ff = _FakeFieldFile('exact.png', payload, size=MAX_EXPORT_IMAGE_BYTES)
        self.assertIsNotNone(read_image_as_base64(ff))

    def test_unreadable_size_returns_none(self):
        ff = _FakeFieldFile('bad.png', b'', size_error=OSError('gone'))
        self.assertIsNone(read_image_as_base64(ff))

    def test_no_extension_uses_octet_stream(self):
        """
        A filename with no extension always returns (None, None)
        from mimetypes.guess_type, so the fallback branch is
        exercised deterministically on every host.
        """
        ff = _FakeFieldFile('noext', b'data')
        result = read_image_as_base64(ff)
        self.assertEqual(result['mime'], 'application/octet-stream')

    def test_filename_is_basename_only(self):
        ff = _FakeFieldFile(
            'question_images/2026/01/deep.png', b'data',
        )
        result = read_image_as_base64(ff)
        self.assertEqual(result['filename'], 'deep.png')


class ApplyImageTests(CacheClearingTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('author')
        self.q = make_question(owner=self.user)

    def test_valid_base64_saves_to_question(self):
        payload = b'fake-image-bytes'
        block = {
            'filename': 'imported.png',
            'mime': 'image/png',
            'data_base64': base64.b64encode(payload).decode('ascii'),
        }
        result = apply_image(self.q, block)
        self.assertTrue(result)
        self.q.refresh_from_db()
        self.assertTrue(self.q.image)

    def test_invalid_base64_returns_false(self):
        block = {
            'filename': 'bad.png',
            'mime': 'image/png',
            'data_base64': 'not-valid-base64!!!',
        }
        self.assertFalse(apply_image(self.q, block))
        self.q.refresh_from_db()
        self.assertFalse(self.q.image)

    def test_missing_data_key_returns_false(self):
        block = {'filename': 'no-data.png'}
        self.assertFalse(apply_image(self.q, block))

    def test_missing_filename_uses_default(self):
        payload = b'data'
        block = {
            'data_base64': base64.b64encode(payload).decode('ascii'),
        }
        result = apply_image(self.q, block)
        self.assertTrue(result)
        self.q.refresh_from_db()
        self.assertTrue(self.q.image)