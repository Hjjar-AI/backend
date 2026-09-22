"""Upload staging shared by the three import formats."""

from django.conf import settings

from apps.core.artifacts import reserve_artifact_path


def stage_upload(file, suffix):
    path = reserve_artifact_path(settings.UPLOAD_FOLDER, 'import', suffix)
    try:
        with path.open('wb') as destination:
            for chunk in file.chunks():
                destination.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def cleanup_staged_upload(path):
    path.unlink(missing_ok=True)
