"""The report blob cache.

This is what makes App Store Connect's brutally rate-limited sales endpoint
usable at all, so the policy has to be right in one specific way: a month that is
over never changes and is cached forever, while the month in progress is
rewritten daily and must not be served stale. Getting that backwards means a
revenue answer that is quietly frozen partway through the month.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from storepilot.core.cache import (
    DAILY,
    FOREVER,
    HOURLY,
    NEVER,
    CachePolicy,
    FileCache,
    monthly_policy,
)


@pytest.fixture
def cache(tmp_path: Path) -> FileCache:
    return FileCache("test", root=tmp_path / "cache")


def test_a_blob_round_trips(cache: FileCache) -> None:
    assert cache.get("sales/2026-07-01") is None
    cache.set("sales/2026-07-01", b"payload", FOREVER)
    assert cache.get("sales/2026-07-01") == b"payload"


def test_keys_of_any_length_and_shape_become_valid_filenames(cache: FileCache) -> None:
    key = "sales/" + "x" * 500 + "/../weird key?"
    cache.set(key, b"ok", FOREVER)
    assert cache.get(key) == b"ok"
    assert cache.path_for(key).parent == cache.root, "no key may escape the namespace directory"


def test_distinct_keys_do_not_collide(cache: FileCache) -> None:
    cache.set("sales/2026-07-01", b"one", FOREVER)
    cache.set("sales/2026-07-02", b"two", FOREVER)
    assert cache.get("sales/2026-07-01") == b"one"
    assert cache.get("sales/2026-07-02") == b"two"


def test_an_expired_entry_is_a_miss_not_stale_data(cache: FileCache) -> None:
    cache.set("today", b"partial", HOURLY)
    assert cache.get("today") is not None
    assert cache.get("today", now=_hours_from_now(2)) is None


def test_a_never_policy_writes_nothing(cache: FileCache) -> None:
    cache.set("volatile", b"payload", NEVER)
    assert cache.get("volatile") is None


def test_a_disabled_cache_is_transparent(tmp_path: Path) -> None:
    disabled = FileCache("test", root=tmp_path / "cache", enabled=False)
    disabled.set("k", b"v", FOREVER)
    assert disabled.get("k") is None


def test_get_or_fetch_calls_the_fetcher_exactly_once(cache: FileCache) -> None:
    calls: list[int] = []

    def fetch() -> bytes:
        calls.append(1)
        return b"fetched"

    assert cache.get_or_fetch("k", fetch, FOREVER) == b"fetched"
    assert cache.get_or_fetch("k", fetch, FOREVER) == b"fetched"
    assert calls == [1]


def test_a_corrupt_metadata_file_is_a_miss_rather_than_a_crash(cache: FileCache) -> None:
    cache.set("k", b"v", FOREVER)
    cache.path_for("k").with_suffix(".meta.json").write_text("{not json")
    assert cache.get("k") is None


def test_invalidate_and_clear(cache: FileCache) -> None:
    cache.set("a", b"1", FOREVER)
    cache.set("b", b"2", FOREVER)
    cache.invalidate("a")
    assert cache.get("a") is None and cache.get("b") == b"2"

    assert cache.clear() == 1
    assert cache.get("b") is None
    assert cache.clear() == 0


def test_a_write_failure_is_never_fatal_to_the_caller(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    cache = FileCache("test", root=locked / "cache")
    try:
        cache.set("k", b"v", FOREVER)  # must not raise
        assert cache.get("k") is None
    finally:
        locked.chmod(0o700)


# --- The month policy --------------------------------------------------------


def test_a_finished_month_is_cached_forever() -> None:
    assert monthly_policy("2026-07", today=date(2026, 8, 1)) == FOREVER
    assert monthly_policy("202606", today=date(2026, 8, 1)) == FOREVER
    assert monthly_policy(date(2026, 5, 15), today=date(2026, 8, 1)) == FOREVER


def test_the_month_in_progress_is_refreshed_daily() -> None:
    """It is still being written; caching it forever freezes revenue mid-month."""
    assert monthly_policy("2026-08", today=date(2026, 8, 15)) == DAILY
    assert monthly_policy("2026-09", today=date(2026, 8, 15)) == DAILY  # the future, too


def test_an_unparseable_month_gets_the_conservative_policy() -> None:
    assert monthly_policy("whenever", today=date(2026, 8, 15)) == DAILY


def test_policy_freshness_arithmetic() -> None:
    assert FOREVER.expires_at(0.0) is None
    assert FOREVER.is_fresh(0.0, now=1e12) is True
    assert DAILY.expires_at(0.0) == 86400
    assert CachePolicy(ttl_seconds=10).is_fresh(100.0, now=109.0) is True
    assert CachePolicy(ttl_seconds=10).is_fresh(100.0, now=111.0) is False


def _hours_from_now(hours: float) -> float:
    import time

    return time.time() + hours * 3600
