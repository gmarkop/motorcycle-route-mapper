"""A tiny TTL cache with an on-disk tier.

Why bother? Two reasons specific to this app:

* The free APIs are shared goodwill infrastructure. Re-requesting the same
  Alpine forecast every time you nudge the map is rude and gets you throttled.
* Route planning happens at the kitchen table but riding happens where the
  signal is bad. Anything already fetched stays readable from disk.

Entries live in memory first (fast, per-process) and on disk second (survives a
restart). Keys are hashed so a long Overpass query becomes a safe filename.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class TTLCache:
    def __init__(self, directory: Path | None = None, namespace: str = "default") -> None:
        self._memory: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._namespace = namespace
        self._dir: Path | None = None
        if directory is not None:
            self._dir = Path(directory) / namespace
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # A read-only home directory should degrade to memory-only
                # caching, not crash the app on startup.
                log.warning("Disk cache unavailable (%s); using memory only.", exc)
                self._dir = None

    # -- public API -----------------------------------------------------------

    def get(self, key: str) -> Any | None:
        """Return the cached value, or ``None`` if absent or expired."""
        digest = self._digest(key)
        now = time.time()

        with self._lock:
            entry = self._memory.get(digest)
        if entry is not None:
            expires_at, value = entry
            if expires_at > now:
                return value
            with self._lock:
                self._memory.pop(digest, None)

        return self._read_disk(digest, now)

    def set(self, key: str, value: Any, ttl_s: int) -> None:
        digest = self._digest(key)
        expires_at = time.time() + max(ttl_s, 0)
        with self._lock:
            self._memory[digest] = (expires_at, value)
        self._write_disk(digest, expires_at, value)

    def get_stale(self, key: str) -> Any | None:
        """Return a cached value even if it has expired.

        Used as a fallback when a live request fails: an hour-old forecast beats
        no forecast at all, as long as the caller says so in the UI.
        """
        digest = self._digest(key)
        with self._lock:
            entry = self._memory.get(digest)
        if entry is not None:
            return entry[1]
        return self._read_disk(digest, now=None)

    def clear(self) -> None:
        with self._lock:
            self._memory.clear()
        if self._dir is not None:
            for path in self._dir.glob("*.json"):
                path.unlink(missing_ok=True)

    # -- internals ------------------------------------------------------------

    def _digest(self, key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    def _path(self, digest: str) -> Path | None:
        return None if self._dir is None else self._dir / f"{digest}.json"

    def _read_disk(self, digest: str, now: float | None) -> Any | None:
        path = self._path(digest)
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return None

        expires_at = float(payload.get("expires_at", 0))
        value = payload.get("value")
        if now is not None and expires_at <= now:
            return None

        with self._lock:
            self._memory[digest] = (expires_at, value)
        return value

    def _write_disk(self, digest: str, expires_at: float, value: Any) -> None:
        path = self._path(digest)
        if path is None:
            return
        try:
            # Write to a temporary file and rename, so a crash mid-write cannot
            # leave a half-written entry that later parses as valid JSON.
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"expires_at": expires_at, "value": value}), "utf-8")
            tmp.replace(path)
        except (OSError, TypeError) as exc:
            log.debug("Could not persist cache entry: %s", exc)
