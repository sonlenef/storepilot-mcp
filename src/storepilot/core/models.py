"""Store-agnostic domain models. Both adapters normalize into these shapes
so portfolio and cross-store tools never deal with raw API payloads."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Store(str, Enum):
    GOOGLE_PLAY = "google_play"
    APP_STORE = "app_store"


class App(BaseModel):
    store: Store
    app_id: str  # package name (Play) / Apple app ID
    name: str


class VitalsSnapshot(BaseModel):
    """Quality metrics with Google's bad-behaviour thresholds applied
    (crash: 1.09% user-perceived, ANR: 0.47%)."""

    store: Store
    app_id: str
    period_end: date
    crash_rate: float | None = None
    anr_rate: float | None = None
    exceeds_crash_threshold: bool = False
    exceeds_anr_threshold: bool = False


class Review(BaseModel):
    store: Store
    app_id: str
    review_id: str
    rating: int
    text: str | None = None
    author: str | None = None
    updated_at: datetime | None = None
    has_developer_reply: bool = False


class Freshness(BaseModel):
    """How current a dataset is, and why it might not be.

    Store report data is never live. Play stats land 3-7 days late and monthly
    earnings land around the 5th of the following month; App Store Connect sales
    reports lag by about a day. Tools must surface this instead of reporting a
    confident "0 installs" for a period that simply has not been published yet.
    """

    as_of: date | None = None
    requested_period: str | None = None  # "2026-07" or "2026-07-01..2026-07-31"
    source: str | None = None  # "play_gcs_reports", "play_reporting_api", "asc_sales", ...
    lag_days: int | None = None
    is_complete: bool = True
    caveat: str | None = None

    @property
    def is_stale(self) -> bool:
        return not self.is_complete or self.caveat is not None

    def warning(self) -> str | None:
        """One-line warning to prepend to a tool response, or None if data is solid."""
        if not self.is_stale:
            return None
        parts = [self.caveat] if self.caveat else []
        if self.as_of:
            parts.append(f"Data as of {self.as_of.isoformat()}.")
        return " ".join(parts) or None

    @classmethod
    def fresh(cls, source: str, as_of: date | None = None) -> Freshness:
        return cls(
            source=source,
            as_of=as_of or datetime.now(UTC).date(),
            is_complete=True,
        )


class ReportRow(BaseModel):
    """One row of a normalized stats/earnings report.

    ``dimension``/``dimension_value`` carry the breakdown a Play report was sliced
    by (country, device, android_os_version, ...) and are None for overview rows.
    """

    store: Store
    app_id: str
    period: date
    metric: str  # e.g. "installs", "revenue_usd", "active_devices"
    value: float
    dimension: str | None = None
    dimension_value: str | None = None
    currency: str | None = None


class Report(BaseModel):
    """A parsed report: rows plus the caveat about how current they are."""

    rows: list[ReportRow] = Field(default_factory=list)
    freshness: Freshness = Field(default_factory=Freshness)
    source_object: str | None = None  # GCS object path / ASC report id it came from

    def total(self, metric: str) -> float:
        return sum(r.value for r in self.rows if r.metric == metric)

    def metrics(self) -> list[str]:
        seen: dict[str, None] = {}
        for row in self.rows:
            seen.setdefault(row.metric, None)
        return list(seen)


class ReleaseStatus(str, Enum):
    DRAFT = "draft"
    IN_PROGRESS = "inProgress"
    HALTED = "halted"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class Release(BaseModel):
    """A release on a track (Play) or a version in a release state (App Store).

    ``track`` is the Play track name ("production", "beta", "alpha", "internal")
    and is set to "appstore" or "testflight" for Apple so cross-store tools can
    group by it uniformly.
    """

    store: Store
    app_id: str
    track: str
    version_name: str | None = None  # user-visible version, e.g. "3.2.1"
    version_codes: list[str] = Field(default_factory=list)  # build numbers
    status: ReleaseStatus = ReleaseStatus.UNKNOWN
    user_fraction: float | None = None  # staged rollout share, 0.0-1.0; None = full
    release_notes: dict[str, str] = Field(default_factory=dict)  # locale -> text
    released_at: datetime | None = None

    @property
    def is_staged_rollout(self) -> bool:
        return self.user_fraction is not None and 0.0 < self.user_fraction < 1.0


class ListingText(BaseModel):
    """Localized store listing copy. Length limits differ per store, so callers
    validate against the target store rather than assuming a shared maximum."""

    store: Store
    app_id: str
    locale: str  # BCP-47-ish: Play uses "en-US", Apple uses "en-US" too
    title: str | None = None
    short_description: str | None = None  # Play: 80 chars; Apple: subtitle, 30 chars
    full_description: str | None = None  # Play: 4000 chars; Apple: description, 4000
    keywords: str | None = None  # Apple only; Play has no keyword field
    video_url: str | None = None


class PortfolioEntry(BaseModel):
    """One row of the unified cross-store portfolio view.

    This is what ``portfolio_overview`` joins on, so every field is optional
    except identity: an app may be missing vitals, revenue, or a live release
    depending on which permissions and reports are available.
    """

    store: Store
    app_id: str
    name: str
    live_version: str | None = None
    live_track: str | None = None
    rollout_fraction: float | None = None
    average_rating: float | None = None
    rating_count: int | None = None
    installs_last_30d: float | None = None
    active_devices: float | None = None
    revenue_last_month: float | None = None
    revenue_currency: str | None = None
    crash_rate: float | None = None
    anr_rate: float | None = None
    health_flags: list[str] = Field(default_factory=list)
    freshness: Freshness = Field(default_factory=Freshness)
