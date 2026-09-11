"""File-based response cache for CourtListener MCP (FR-4, ADR-2).

Plain JSON files under a configurable root (default ``.cache/court-listener/``),
namespaced: ``citations/``, ``clusters/``, ``dockets/``, ``opinions/``.

Entry shape: ``{"cached_at": <unix ts>, "data": <payload>}``.

Policy (D1): ``citations``/``clusters``/``opinions`` share the long "static"
TTL (default 30 days — effectively immutable); ``dockets`` use the short TTL
(default 24 hours — mutable). A total size cap (default 100 MB) evicts
oldest-mtime files first; the eviction scan runs only on ``put`` — ``get`` is
a single file read, never an O(n) walk.

Reliability (NFR-6): unreadable/corrupt entries are treated as misses, never
fatal. Writes are atomic (temp file + ``os.replace``, FR-4.6). Only response
data is stored — no auth material ever reaches a cache file (AC-4.6).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import config

# The four FR-4.1 namespaces; everything except dockets is "static"-TTL'd.
NAMESPACES: frozenset[str] = frozenset({"citations", "clusters", "dockets", "opinions"})
_STATIC_NAMESPACES: frozenset[str] = frozenset({"citations", "clusters", "opinions"})

# D1 defaults; used only if the Config fields themselves are unavailable.
SECONDS_PER_DAY = 86400
DEFAULT_TTL_STATIC = 30 * SECONDS_PER_DAY  # 30 days
DEFAULT_TTL_DOCKETS = 24 * 3600  # 24 hours
DEFAULT_MAX_MB = 100  # 100 MB

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify_key(key: str) -> str:
    """Slugify a citation string into a filesystem-safe cache key."""
    slug = _SLUG_RE.sub("-", key.strip().lower()).strip("-")
    return slug or "empty"


_cache_singleton: FileCache | None = None


def get_cache() -> FileCache:
    """Return the process-wide cache, created from Config on first use."""
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = FileCache()
    return _cache_singleton


def set_cache(cache: FileCache | None) -> None:
    """Swap the process-wide cache; None clears it. Test-isolation seam only."""
    global _cache_singleton
    _cache_singleton = cache


class FileCache:
    """Namespaced JSON file cache with per-class TTL and a size cap.

    Args:
        root: Cache root directory (default: ``config.courtlistener_cache_dir``).
        ttl_static: TTL seconds for citations/clusters/opinions
            (default: ``config.courtlistener_cache_ttl_static``).
        ttl_dockets: TTL seconds for dockets
            (default: ``config.courtlistener_cache_ttl_dockets``).
        max_mb: Total size cap across all namespaces in MB
            (default: ``config.courtlistener_cache_max_mb``).

    """

    def __init__(
        self,
        root: str | Path | None = None,
        ttl_static: int | None = None,
        ttl_dockets: int | None = None,
        max_mb: int | None = None,
    ) -> None:
        """Create a cache bound to the given (or configured) root."""
        self.root = Path(root) if root is not None else Path(config.courtlistener_cache_dir)
        self.ttl_static = (
            ttl_static if ttl_static is not None else config.courtlistener_cache_ttl_static
        )
        self.ttl_dockets = (
            ttl_dockets if ttl_dockets is not None else config.courtlistener_cache_ttl_dockets
        )
        self.max_mb = max_mb if max_mb is not None else config.courtlistener_cache_max_mb

    # -- paths ------------------------------------------------------------

    def _path(self, ns: str, key: str) -> Path:
        if ns not in NAMESPACES:
            raise ValueError(f"Unknown cache namespace: {ns!r}")
        return self.root / ns / f"{key}.json"

    def _ensure_root(self) -> None:
        """Create the root directory with user-only permissions (NFR-4)."""
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)

    # -- core API -----------------------------------------------------------

    def _ttl_for(self, ns: str) -> int:
        return self.ttl_static if ns in _STATIC_NAMESPACES else self.ttl_dockets

    def get(self, ns: str, key: str) -> Any | None:
        """Return cached ``data`` for ``(ns, key)``, or None on miss/expiry/corruption.

        Expiry rule: ``now - cached_at > ttl`` (strictly older than TTL is a
        miss). Unreadable, invalid-JSON, or malformed entries are misses.

        """
        path = self._path(ns, key)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            cached_at = float(entry["cached_at"])
            data = entry["data"]
        except (OSError, ValueError, KeyError, TypeError):
            # Missing file, unreadable file, corrupt JSON, wrong shape → miss.
            return None

        ttl = self._ttl_for(ns)
        age = time.time() - cached_at
        if age > ttl:  # expired = now - cached_at > ttl (single comparison site)
            return None
        return data

    def put(self, ns: str, key: str, data: Any) -> None:
        """Atomically write ``data`` for ``(ns, key)``, then evict to the cap.

        Atomicity (FR-4.6): write to a sibling temp file, chmod user-only,
        then ``os.replace`` so a concurrent reader never sees partial JSON.

        """
        if ns not in NAMESPACES:
            raise ValueError(f"Unknown cache namespace: {ns!r}")
        self._ensure_root()
        path = self._path(ns, key)
        path.parent.mkdir(parents=True, exist_ok=True)

        entry = {"cached_at": time.time(), "data": data}
        payload = json.dumps(entry, ensure_ascii=False, default=str)
        tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

        self._evict_to_cap()

    # -- eviction -----------------------------------------------------------

    def _evict_to_cap(self) -> None:
        """Delete oldest-mtime files until total size ≤ cap (D1, AC-4.4).

        Only called from ``put`` — ``get`` never scans the tree (NFR-2/low
        latency: no O(n) walk on the hot path).

        """
        cap_bytes = self.max_mb * 1024 * 1024
        files: list[Path] = []
        try:
            files = [p for p in self.root.rglob("*.json") if p.is_file()]
            sizes = {p: p.stat().st_size for p in files}
        except OSError:
            return  # can't even stat the tree; leave it be (self-heals later)
        total = sum(sizes.values())
        if total <= cap_bytes:
            return
        for path in sorted(files, key=lambda p: p.stat().st_mtime):
            if total <= cap_bytes:
                break
            size = sizes[path]
            try:
                path.unlink()
                total -= size
            except OSError:
                continue  # can't delete → don't adjust the accounting
