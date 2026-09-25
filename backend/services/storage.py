"""Local-disk object storage (replaces the old hosted object store).

Files live under STORAGE_DIR (default: <repo>/storage). Object paths look like
"storyforge/uploads/<book_id>/<filename>" and are what gets saved in MongoDB as
`storage_path`, so the interface (put_object / get_object) is unchanged.
"""
import mimetypes
import os
from pathlib import Path

APP_NAME = "storyforge"
STORAGE_ROOT = Path(os.environ.get("STORAGE_DIR") or Path(__file__).resolve().parent.parent.parent / "storage")


def _resolve(path: str) -> Path:
    """Map an object path to a file under STORAGE_ROOT, refusing anything that escapes it."""
    root = STORAGE_ROOT.resolve()
    target = (root / str(path).lstrip("/")).resolve()
    if root != target and root not in target.parents:
        raise ValueError("invalid storage path")
    return target


def put_object(path: str, data: bytes, content_type: str = "application/octet-stream") -> dict:
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": str(path).lstrip("/"), "size": len(data), "content_type": content_type}


def get_object(path: str):
    target = _resolve(path)
    if not target.is_file():
        raise FileNotFoundError(f"object not found: {path}")
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return target.read_bytes(), content_type
