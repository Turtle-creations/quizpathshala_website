from pathlib import Path
import uuid

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from config import BASE_DIR, IMAGE_DIR


ALLOWED_QUESTION_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
QUESTION_IMAGE_UPLOAD_DIR = (IMAGE_DIR / "questions").resolve()


def save_question_image(upload: FileStorage | None) -> str | None:
    if upload is None:
        return None

    filename = secure_filename(upload.filename or "")
    if not filename:
        return None

    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix not in ALLOWED_QUESTION_IMAGE_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_QUESTION_IMAGE_EXTENSIONS))
        raise ValueError(f"Unsupported image format. Please upload one of: {allowed}.")

    QUESTION_IMAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}.{suffix}"
    destination = QUESTION_IMAGE_UPLOAD_DIR / stored_name
    upload.save(destination)
    return destination.relative_to(BASE_DIR).as_posix()


def delete_question_image(relative_path: str | None) -> None:
    if not relative_path:
        return

    candidate = (BASE_DIR / relative_path).resolve()
    if QUESTION_IMAGE_UPLOAD_DIR not in candidate.parents:
        return
    if candidate.is_file():
        candidate.unlink(missing_ok=True)
