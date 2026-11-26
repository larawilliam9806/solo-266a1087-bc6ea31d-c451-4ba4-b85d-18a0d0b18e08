"""Server-side snapshot storage.

The HTML workbench keeps only an opaque *reference* to an already accepted
snapshot in its forms.  Posting the whole normalized JSON back and forth both
amplifies the body (HTML/form encoding can push an accepted snapshot over the
upload size limit) and risks losing exact byte precision.

Records are stored as JSON files in a shared directory so that independent
gunicorn workers (or separate app instances pointing at the same directory)
resolve the same token.  Each upload gets an immutable token, which gives
every browser tab its own snapshot.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

# Best-effort retention for stale records.
_TTL_SECONDS = 24 * 60 * 60
_CLEANUP_PROBABILITY = 1.0 / 50.0


class SnapshotStoreError(RuntimeError):
    """Raised when a snapshot record cannot be persisted."""


def store_directory() -> Path:
    """Return the shared directory used for snapshot records."""

    configured = os.environ.get("SNAPSHOT_STORE_DIR", "").strip()
    if configured:
        directory = Path(configured)
    else:
        directory = Path(tempfile.gettempdir()) / "k8s-recovery-lab-snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def is_token(value: Any) -> bool:
    return isinstance(value, str) and bool(_TOKEN_RE.fullmatch(value.strip()))


def _record_path(token: str) -> Path:
    return store_directory() / f"{token}.json"


def create_snapshot(snapshot: dict[str, Any]) -> str | None:
    """Persist a canonical snapshot and return its token.

    Returns ``None`` when the record cannot be written; callers can fall back
    to stateless inline JSON in that case.
    """

    token = secrets.token_urlsafe(18)
    record = {
        "token": token,
        "createdAt": time.time(),
        "snapshot": snapshot,
    }
    try:
        path = _record_path(token)
        temporary = path.with_name(f".{token}.tmp-{os.getpid()}")
        temporary.write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    except OSError:
        return None

    if secrets.randbelow(10_000) < int(_CLEANUP_PROBABILITY * 10_000):
        cleanup_expired()
    return token


def load_snapshot(token: str | None) -> dict[str, Any] | None:
    """Load a canonical snapshot by token; ``None`` if unavailable."""

    if not is_token(token):
        return None
    assert token is not None
    try:
        path = _record_path(token.strip())
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    snapshot = record.get("snapshot") if isinstance(record, dict) else None
    return snapshot if isinstance(snapshot, dict) else None


def cleanup_expired(*, max_age_seconds: int = _TTL_SECONDS) -> int:
    """Remove records older than the retention window. Returns removed count."""

    try:
        directory = store_directory()
    except OSError:
        return 0

    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        entries = list(directory.glob("*.json"))
    except OSError:
        return 0
    for path in entries:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed
