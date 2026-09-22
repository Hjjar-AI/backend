"""Reserve distinct paths for generated downloads and backup files."""

import os
import tempfile
from pathlib import Path

from django.utils import timezone


def reserve_artifact_path(directory, prefix, suffix):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
    fd, path = tempfile.mkstemp(
        prefix=f'{prefix}_{timestamp}_', suffix=suffix, dir=directory,
    )
    os.close(fd)
    return Path(path)


def download_filename(path):
    """Keep the public timestamp filename while storage uses a unique suffix."""
    path = Path(path)
    return f"{path.stem.rsplit('_', 1)[0]}{path.suffix}"
