from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from .config import DATA_DIR
from .models import utc_now


class InstanceLock:
    """Hold a process-wide advisory lock and publish the active local URL."""

    def __init__(self, path: Path = DATA_DIR / "instance.lock"):
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            self._file = None
            return False
        return True

    def publish(self, url: str) -> None:
        if not self._file:
            raise RuntimeError("Instance lock is not held.")
        self._file.seek(0)
        self._file.truncate()
        json.dump({"pid": os.getpid(), "url": url, "started_at": utc_now()}, self._file)
        self._file.flush()
        os.fsync(self._file.fileno())

    def current_url(self) -> str | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        value = payload.get("url")
        return value if isinstance(value, str) and value.startswith("http://127.0.0.1:") else None

    def close(self) -> None:
        if self._file:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    def __enter__(self) -> InstanceLock:
        if not self.acquire():
            raise RuntimeError("Another instance is already running.")
        return self

    def __exit__(self, *_args) -> None:
        self.close()

