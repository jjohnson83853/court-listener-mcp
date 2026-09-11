"""Tests for app/cache.py — file cache (FR-4, ADR-2).

AC coverage:
- AC-4.2: files appear under the expected namespace paths with expected names.
- AC-4.3: TTL expiry → miss (deterministic via a fake clock; the fresh-HTTP +
  rewrite half is covered tool-level in test_mocked.py::test_ac_4_3).
- AC-4.4: exceeding the size cap evicts oldest-mtime entries first.
- AC-4.6: no cache file ever contains key/auth material.
- Checklist (c): pydantic-settings cache fields honor env overrides.
"""

import json
import os
import stat
from typing import Any

import pytest

import app.cache as cache_module
from app.cache import FileCache, slugify_key
from app.config import Config

DUMMY_KEY = "test-key-offline-dummy"


class _FakeTime:
    """Deterministic time.time replacement."""

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def cache(tmp_path: Any) -> FileCache:
    """A cache rooted in tmp_path with short, deterministic TTLs."""
    return FileCache(root=tmp_path, ttl_static=3600, ttl_dockets=60, max_mb=100)


class TestPathsAndEntries:
    """AC-4.2 — namespace paths, file names, entry shape."""

    def test_ac_4_2_file_paths_and_names(self, cache: FileCache) -> None:
        """AC-4.2: put() creates <root>/<ns>/<key>.json with {cached_at,data}."""
        cache.put("clusters", "cluster-123", {"caseName": "X"})
        expected = cache.root / "clusters" / "cluster-123.json"
        assert expected.is_file()

        raw = json.loads(expected.read_text(encoding="utf-8"))
        assert set(raw.keys()) == {"cached_at", "data"}
        assert raw["data"] == {"caseName": "X"}
        assert isinstance(raw["cached_at"], float)

    def test_ac_4_2_all_four_namespaces(self, tmp_path: Any) -> None:
        """AC-4.2: each FR-4.1 namespace gets its own directory."""
        for ns in ("citations", "clusters", "dockets", "opinions"):
            FileCache(root=tmp_path).put(ns, "k1", {"v": ns})
            assert (tmp_path / ns / "k1.json").is_file()

    def test_roundtrip(self, cache: FileCache) -> None:
        """Golden path: put → get returns the same data."""
        payload = {"a": 1, "b": ["x", "y"]}
        cache.put("opinions", "opinion-9", payload)
        assert cache.get("opinions", "opinion-9") == payload

    def test_unknown_namespace_rejected(self, cache: FileCache) -> None:
        """Typos must not create stray directories."""
        with pytest.raises(ValueError, match="namespace"):
            cache.get("bogus", "k")
        with pytest.raises(ValueError, match="namespace"):
            cache.put("bogus", "k", {"v": 1})

    def test_slugify_key(self) -> None:
        """Citation strings slugify to filesystem-safe keys."""
        assert slugify_key("410 U.S. 113") == "410-u-s-113"
        assert slugify_key("  R. v. O'Connor  ") == "r-v-o-connor"
        assert slugify_key("!!!") == "empty"


class TestTTL:
    """AC-4.3 — expiry behaves as a miss; rewrite refreshes."""

    def test_ac_4_3_static_ttl_expiry_is_a_miss(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-4.3: expired entry (now - cached_at > ttl) reads as a miss."""
        fake_time = _FakeTime(1_000_000.0)
        monkeypatch.setattr(cache_module.time, "time", fake_time)
        cache = FileCache(root=tmp_path, ttl_static=3600, ttl_dockets=60, max_mb=100)

        cache.put("clusters", "cluster-9", {"v": 1})
        assert cache.get("clusters", "cluster-9") == {"v": 1}

        # Boundary: age == ttl exactly → still fresh (strict > comparison).
        fake_time.now += 3600
        assert cache.get("clusters", "cluster-9") == {"v": 1}

        # age = ttl + 1 → expired → miss.
        fake_time.now += 1
        assert cache.get("clusters", "cluster-9") is None

    def test_ac_4_3_rewrite_after_expiry(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-4.3: after expiry, a put() refreshes cached_at → hit again."""
        fake_time = _FakeTime(1_000_000.0)
        monkeypatch.setattr(cache_module.time, "time", fake_time)
        cache = FileCache(root=tmp_path, ttl_static=60, ttl_dockets=60, max_mb=100)

        cache.put("clusters", "cluster-9", {"v": 1})
        fake_time.now += 61
        assert cache.get("clusters", "cluster-9") is None

        cache.put("clusters", "cluster-9", {"v": 2})  # rewrite with fresh ts
        assert cache.get("clusters", "cluster-9") == {"v": 2}

    def test_dockets_use_short_ttl(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D1: dockets expire at ttl_dockets even when static TTL is huge."""
        fake_time = _FakeTime(1_000_000.0)
        monkeypatch.setattr(cache_module.time, "time", fake_time)
        cache = FileCache(root=tmp_path, ttl_static=3600, ttl_dockets=60, max_mb=100)

        cache.put("dockets", "docket-1", {"v": 1})
        fake_time.now += 61  # past docket TTL, far inside static TTL
        assert cache.get("dockets", "docket-1") is None

        cache.put("clusters", "cluster-1", {"v": 1})
        fake_time.now += 61
        assert cache.get("clusters", "cluster-1") == {"v": 1}  # static still fresh


class TestCorruption:
    """NFR-6 — invalid entries are misses, never fatal."""

    def test_corrupt_json_is_a_miss(self, cache: FileCache) -> None:
        """Corrupt JSON → None, never an exception."""
        path = cache.root / "clusters" / "bad.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert cache.get("clusters", "bad") is None

    def test_malformed_entry_is_a_miss(self, cache: FileCache) -> None:
        """Valid JSON but wrong entry shape → miss."""
        path = cache.root / "dockets" / "weird.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"nope": 1}', encoding="utf-8")
        assert cache.get("dockets", "weird") is None

    def test_missing_file_is_a_miss(self, cache: FileCache) -> None:
        """No file at all → None (the plain miss)."""
        assert cache.get("citations", "never-written") is None

    def test_no_tmp_files_left_after_put(self, cache: FileCache) -> None:
        """FR-4.6: atomic replace leaves no temp files behind."""
        for _ in range(5):
            cache.put("clusters", "cluster-1", {"v": 1})
        leftovers = [p.name for p in cache.root.rglob("*.tmp")]
        assert leftovers == []


class TestEviction:
    """AC-4.4 — oldest-first eviction at the size cap (D1)."""

    def test_ac_4_4_cap_evicts_oldest_first(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-4.4: over-cap evicts by oldest mtime until total ≤ cap."""
        fake_time = _FakeTime(1_000_000.0)
        monkeypatch.setattr(cache_module.time, "time", fake_time)
        cache = FileCache(root=tmp_path, ttl_static=3600, ttl_dockets=60, max_mb=100)
        keys = ["cluster-1", "cluster-2", "cluster-3"]
        for key in keys:
            cache.put("clusters", key, {"data": "x" * 50})

        # Deterministic mtime ordering: 1 oldest, 3 newest.
        base = fake_time.now - 10000
        for i, key in enumerate(keys):
            os.utime(tmp_path / "clusters" / f"{key}.json", (base + i * 100, base + i * 100))

        sizes = {
            key: (tmp_path / "clusters" / f"{key}.json").stat().st_size for key in keys
        }
        # Cap fits exactly the two newest entries.
        cap_bytes = sizes["cluster-2"] + sizes["cluster-3"]
        cache.max_mb = cap_bytes / (1024 * 1024)

        cache.put("clusters", "cluster-4", {"data": "x" * 50})

        remaining = sorted(p.name for p in (tmp_path / "clusters").glob("*.json"))
        assert remaining == ["cluster-3.json", "cluster-4.json"]

    def test_eviction_scan_only_on_put(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Checklist (e): get() must never run the O(n) eviction scan."""
        cache = FileCache(root=tmp_path, ttl_static=3600, ttl_dockets=60, max_mb=1)
        cache.put("clusters", "cluster-1", {"v": 1})

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("eviction scan must not run on get()")

        monkeypatch.setattr(FileCache, "_evict_to_cap", _boom)
        assert cache.get("clusters", "cluster-1") == {"v": 1}


class TestSecurityAndPerms:
    """AC-4.6 + NFR-4 — no auth material, user-only permissions."""

    def test_ac_4_6_no_key_material_in_entries(self, cache: FileCache) -> None:
        """AC-4.6: serialized entries never contain key/header material."""
        cache.put(
            "citations",
            slugify_key("410 U.S. 113"),
            {"citation": "410 U.S. 113", "cluster": {"caseName": "Roe v. Wade"}},
        )
        for path in cache.root.rglob("*.json"):
            content = path.read_text(encoding="utf-8")
            assert DUMMY_KEY not in content
            assert "Authorization" not in content
            assert "Token " not in content

    def test_no_auth_headers_storable(self, cache: FileCache) -> None:
        """AC-4.6 boundary: FileCache stores payloads verbatim, so the guard is
        that tools never pass auth material. Assert a normal data payload round-trips
        and that nothing beyond it is appended to the entry."""
        cache.put("citations", "k", {"caseName": "Roe v. Wade"})
        raw = (cache.root / "citations" / "k.json").read_text(encoding="utf-8")
        entry = json.loads(raw)
        assert set(entry.keys()) == {"cached_at", "data"}  # nothing else sneaks in
        assert entry["data"] == {"caseName": "Roe v. Wade"}

    def test_directory_permissions_user_only(self, tmp_path: Any) -> None:
        """NFR-4: freshly created cache root is 0o700, files 0o600."""
        cache = FileCache(root=tmp_path / "deep" / "cache")
        cache.put("clusters", "cluster-1", {"v": 1})
        mode = stat.S_IMODE((tmp_path / "deep" / "cache").stat().st_mode)
        assert mode == 0o700
        file_mode = stat.S_IMODE((tmp_path / "deep" / "cache" / "clusters" / "cluster-1.json").stat().st_mode)
        assert file_mode == 0o600


class TestConfigIntegration:
    """T3 — Config fields exist, default, and honor env overrides (D1)."""

    def test_config_defaults_match_d1(self) -> None:
        """D1 defaults: 30d static / 24h dockets / 100 MB / .cache/court-listener/."""
        cfg = Config(_env_file=None)
        assert cfg.courtlistener_cache_dir == ".cache/court-listener/"
        assert cfg.courtlistener_cache_ttl_static == 30 * 86400
        assert cfg.courtlistener_cache_ttl_dockets == 86400
        assert cfg.courtlistener_cache_max_mb == 100

    def test_cache_config_env_overrides(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """Checklist (c): env vars actually override the Config defaults."""
        monkeypatch.setenv("COURTLISTENER_CACHE_DIR", str(tmp_path / "envdir"))
        monkeypatch.setenv("COURTLISTENER_CACHE_TTL_STATIC", "123")
        monkeypatch.setenv("COURTLISTENER_CACHE_TTL_DOCKETS", "7")
        monkeypatch.setenv("COURTLISTENER_CACHE_MAX_MB", "5")
        cfg = Config(_env_file=None)
        assert cfg.courtlistener_cache_dir == str(tmp_path / "envdir")
        assert cfg.courtlistener_cache_ttl_static == 123
        assert cfg.courtlistener_cache_ttl_dockets == 7
        assert cfg.courtlistener_cache_max_mb == 5

    def test_filecache_reads_config_when_unspecified(self) -> None:
        """FileCache() without args falls back to the configured root/defaults."""
        from app.config import config as live_config

        cache = FileCache()
        assert cache.ttl_static == live_config.courtlistener_cache_ttl_static
        assert cache.ttl_dockets == live_config.courtlistener_cache_ttl_dockets
        assert cache.max_mb == live_config.courtlistener_cache_max_mb
        assert str(cache.root).endswith("court-listener")
