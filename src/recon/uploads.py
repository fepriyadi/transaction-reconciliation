"""Where uploaded batch files are kept."""
from __future__ import annotations

import hashlib
from pathlib import Path


def save_upload(name: str, data: bytes, folder: Path) -> Path:
    """Content-addressed (<folder>/<sha12>/<name>), so two different files with the
    same name never overwrite each other and re-uploading the same file is a no-op."""
    p = Path(folder) / hashlib.sha256(data).hexdigest()[:12] / Path(name).name
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return p
