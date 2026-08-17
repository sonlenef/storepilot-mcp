"""Local blob cache: key -> bytes, with an explicit TTL policy per entry.

Both stores need this for the same reason. Play report objects live in a GCS
bucket where every read costs latency, and App Store Connect's sales endpoint is
severely rate limited (a handful of requests per hour before it starts refusing).
Monthly reports for a past month never change again, so they are cached forever;
the current month is rewritten daily and gets a short TTL.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from storepilot.core.guards import PRIVATE_FILE_MODE, ensure_private_dir

DEFAULT_CACHE_ROOT = Path.home() / ".storepilot" / "cache"

ONE_DAY = 24 * 60 * 60


@dataclass(frozen=True)
class CachePolicy:
    """``ttl_seconds=None`` means the entry never expires."""

    ttl_seconds: float | None = None

    def expires_at(self, stored_at: float) -> float | None:
        if self.ttl_seconds is None:
            return None
        return stored_at + self.ttl_seconds

    def is_fresh(self, stored_at: float, *, now: float | None = None) -> bool:
        expiry = self.expires_at(stored_at)
        if expiry is None:
            return True
        return (now if now is not None else time.time()) < expiry


FOREVER = CachePolicy(ttl_seconds=None)
DAILY = CachePolicy(ttl_seconds=ONE_DAY)
HOURLY = CachePolicy(ttl_seconds=60 * 60)
NEVER = CachePolicy(ttl_seconds=0)


def monthly_policy(month: str | date, *, today: date | None = None) -> CachePolicy:
    """Immutable-forever for past months, one day for the month still in progress.

    ``month`` accepts "2026-07", "202607", or any date inside the month.
    """
    today = today or datetime.now(UTC).date()
    if isinstance(month, date):
        year, mon = month.year, month.month
    else:
        digits = re.sub(r"\D", "", month)
        if len(digits) < 6:
            return DAILY
        year, mon = int(digits[:4]), int(digits[4:6])
    if (year, mon) >= (today.year, today.month):
        return DAILY
    return FOREVER


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return cleaned[:64] or "x"


class FileCache:
    """Filesystem-backed blob cache.

    Entries are stored as ``<root>/<namespace>/<slug>-<hash>.bin`` with a sibling
    ``.meta.json`` holding the store time, policy and original key. Keys may be
    arbitrary length; the hash guarantees a valid filename while the slug keeps
    the cache directory human-readable when debugging.
    """

    def __init__(
        self,
        namespace: str = "default",
        *,
        root: Path | None = None,
        enabled: bool = True,
    ) -> None:
        self.namespace = _safe_component(namespace)
        self.root = Path(root or DEFAULT_CACHE_ROOT).expanduser() / self.namespace
        self.enabled = enabled

    def path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        slug = _safe_component(key.rsplit("/", 1)[-1])
        return self.root / f"{slug}-{digest}.bin"

    def _meta_path(self, key: str) -> Path:
        return self.path_for(key).with_suffix(".meta.json")

    def get(self, key: str, *, now: float | None = None) -> bytes | None:
        """Return cached bytes, or None on miss, expiry, or unreadable entry."""
        if not self.enabled:
            return None
        blob_path = self.path_for(key)
        meta_path = self._meta_path(key)
        if not blob_path.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            policy = CachePolicy(ttl_seconds=meta.get("ttl_seconds"))
            if not policy.is_fresh(float(meta["stored_at"]), now=now):
                return None
            return blob_path.read_bytes()
        except (OSError, ValueError, KeyError):
            return None

    def set(self, key: str, data: bytes, policy: CachePolicy = FOREVER) -> None:
        """Write an entry. Cache failures are never fatal to the caller."""
        if not self.enabled or policy.ttl_seconds == 0:
            return
        blob_path = self.path_for(key)
        try:
            ensure_private_dir(blob_path.parent)
            tmp = blob_path.with_suffix(".tmp")
            # These blobs are sales and earnings reports: revenue figures for the
            # operator's whole portfolio. Create them owner-only rather than
            # inheriting a umask that leaves them world-readable.
            tmp.touch(mode=PRIVATE_FILE_MODE, exist_ok=True)
            tmp.chmod(PRIVATE_FILE_MODE)
            tmp.write_bytes(data)
            tmp.replace(blob_path)
            meta_path = self._meta_path(key)
            meta_path.touch(mode=PRIVATE_FILE_MODE, exist_ok=True)
            meta_path.chmod(PRIVATE_FILE_MODE)
            meta_path.write_text(
                json.dumps(
                    {
                        "key": key,
                        "stored_at": time.time(),
                        "ttl_seconds": policy.ttl_seconds,
                        "size": len(data),
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            return

    def get_or_fetch(
        self,
        key: str,
        fetch: Callable[[], bytes],
        policy: CachePolicy = FOREVER,
    ) -> bytes:
        """Return the cached blob, otherwise call ``fetch`` and store its result."""
        cached = self.get(key)
        if cached is not None:
            return cached
        data = fetch()
        self.set(key, data, policy)
        return data

    def invalidate(self, key: str) -> None:
        for path in (self.path_for(key), self._meta_path(key)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def clear(self) -> int:
        """Remove every entry in this namespace. Returns the count of blobs removed."""
        removed = 0
        if not self.root.exists():
            return 0
        for path in self.root.iterdir():
            try:
                if path.suffix == ".bin":
                    removed += 1
                path.unlink()
            except OSError:
                pass
        return removed
