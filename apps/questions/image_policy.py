"""Image limits shared by upload, state import, and state export."""

MAX_IMAGE_SIZE = 5 * 1024 * 1024
ALLOWED_IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
ALLOWED_IMAGE_MIMES = frozenset({
    'image/jpeg', 'image/png', 'image/gif', 'image/webp',
})
