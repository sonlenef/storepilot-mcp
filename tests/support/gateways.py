"""Fakes for the two cross-store adapter seams.

``PlayGateway`` and ``AppleGateway`` exist precisely so the degradation behaviour
of the portfolio can be exercised without a Play account or an Apple key — these
subclasses fill them in from canned data and let any single call be made to fail.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from storepilot.core.models import (
    Freshness,
    Release,
    Report,
    ReportRow,
    Store,
    VitalsSnapshot,
)
from storepilot.cross import tools
from storepilot.cross.apps import StoreApp

MONTH = "2026-07"
PERIOD = date(2026, 7, 31)


# --- Canned payloads ---------------------------------------------------------


class FakeReading:
    """Stands in for ``reporting.MetricReading`` — only ``key``/``error`` are read."""

    def __init__(self, key: str, error: str | None = None) -> None:
        self.key = key
        self.error = error


class FakeVitals:
    """Stands in for ``reporting.VitalsResult``."""

    def __init__(
        self,
        package: str,
        crash: float | None,
        anr: float | None,
        *,
        caveat: str | None = None,
        reading_errors: dict[str, str] | None = None,
    ) -> None:
        self.snapshot = VitalsSnapshot(
            store=Store.GOOGLE_PLAY,
            app_id=package,
            period_end=PERIOD,
            crash_rate=crash,
            anr_rate=anr,
            exceeds_crash_threshold=crash is not None and crash > 1.09,
            exceeds_anr_threshold=anr is not None and anr > 0.47,
        )
        self.freshness = Freshness(
            as_of=PERIOD,
            source="play_reporting_api",
            requested_period="2026-07-04..2026-07-31",
            is_complete=caveat is None,
            caveat=caveat,
        )
        errors = reading_errors or {}
        self.readings = {key: FakeReading(key, errors.get(key)) for key in ("crash", "anr")}


SUPPRESSED_CAVEAT = (
    "No datapoints were returned for this window. Android Vitals suppresses metrics for apps "
    "below a minimum daily user count, so this is expected for a low-traffic app and does not "
    "mean the app is crash-free."
)


def installs_report(package: str, total: float, *, caveat: str | None = None) -> Report:
    return Report(
        rows=[
            ReportRow(
                store=Store.GOOGLE_PLAY,
                app_id=package,
                period=PERIOD,
                metric="daily_device_installs",
                value=total,
            )
        ],
        freshness=Freshness(
            as_of=PERIOD,
            source="play_gcs_reports",
            requested_period=MONTH,
            is_complete=caveat is None,
            caveat=caveat,
        ),
    )


def ratings_report(package: str, value: float) -> Report:
    return Report(
        rows=[
            ReportRow(
                store=Store.GOOGLE_PLAY,
                app_id=package,
                period=PERIOD,
                metric="total_average_rating",
                value=value,
            )
        ],
        freshness=Freshness(as_of=PERIOD, source="play_gcs_reports", is_complete=True),
    )


def earnings_report(
    entries: list[tuple[str, float, str]], *, caveat: str | None = None
) -> Report:
    """``entries`` is ``[(product_id, amount, currency)]`` — Play earnings are per product."""
    return Report(
        rows=[
            ReportRow(
                store=Store.GOOGLE_PLAY,
                app_id=product_id,
                period=PERIOD,
                metric="earnings",
                value=amount,
                currency=currency,
            )
            for product_id, amount, currency in entries
        ],
        freshness=Freshness(
            as_of=PERIOD,
            source="play_gcs_reports",
            requested_period=MONTH,
            is_complete=caveat is None,
            caveat=caveat,
        ),
    )


def sales_report(
    entries: list[tuple[str, float, float, str]], *, caveat: str | None = None
) -> Report:
    """``entries`` is ``[(apple_id, units, proceeds, currency)]``."""
    rows: list[ReportRow] = []
    for apple_id, units, proceeds, currency in entries:
        rows.append(
            ReportRow(
                store=Store.APP_STORE,
                app_id=apple_id,
                period=PERIOD,
                metric="units",
                value=units,
            )
        )
        rows.append(
            ReportRow(
                store=Store.APP_STORE,
                app_id=apple_id,
                period=PERIOD,
                metric="proceeds",
                value=proceeds,
                currency=currency,
            )
        )
    return Report(
        rows=rows,
        freshness=Freshness(
            as_of=PERIOD,
            source="asc_sales",
            requested_period=MONTH,
            lag_days=1,
            is_complete=caveat is None,
            caveat=caveat,
        ),
    )


# --- Gateways ----------------------------------------------------------------


class FakePlay(tools.PlayGateway):
    def __init__(
        self,
        *,
        configured: bool = True,
        bucket: bool = True,
        apps: list[StoreApp] | None = None,
        vitals: dict[str, FakeVitals] | None = None,
        installs: dict[str, Report] | None = None,
        ratings: dict[str, Report] | None = None,
        earnings: Report | None = None,
        releases: dict[str, Release] | None = None,
        fail: dict[tuple[str, str], Exception] | None = None,
    ) -> None:
        self._configured = configured
        self._bucket = bucket
        self._apps = list(apps or [])
        self._vitals = vitals or {}
        self._installs = installs or {}
        self._ratings = ratings or {}
        self._earnings = earnings
        self._releases = releases or {}
        self._fail = dict(fail or {})

    def available(self) -> str | None:
        if not self._configured:
            return (
                "Google Play is not configured. Set STOREPILOT_GOOGLE_CREDENTIALS to a service "
                "account JSON key. Run setup_doctor for the full checklist."
            )
        return None

    def reports_available(self) -> str | None:
        if not self._bucket:
            return (
                "STOREPILOT_GOOGLE_REPORTS_BUCKET is not set, so installs, ratings and earnings "
                "are unavailable for every Play app (no Play REST API exposes them)."
            )
        return None

    def list_apps(self) -> list[StoreApp]:
        if ("list_apps", "") in self._fail:
            raise self._fail[("list_apps", "")]
        return self._apps

    def vitals(self, package: str, days: int) -> Any:
        if ("vitals", package) in self._fail:
            raise self._fail[("vitals", package)]
        # An app with no canned vitals stands in for one Android Vitals has no
        # data for — the common case for a low-traffic or brand-new app.
        return self._vitals.get(package) or FakeVitals(package, None, None, caveat=SUPPRESSED_CAVEAT)

    def installs(self, package: str, month: str) -> Report:
        if ("installs", package) in self._fail:
            raise self._fail[("installs", package)]
        return self._installs.get(package) or installs_report(package, 0)

    def ratings(self, package: str, month: str) -> Report:
        if ("ratings", package) in self._fail:
            raise self._fail[("ratings", package)]
        return self._ratings.get(package) or ratings_report(package, 0.0)

    def earnings(self, month: str) -> Report:
        if ("earnings", "") in self._fail:
            raise self._fail[("earnings", "")]
        return self._earnings if self._earnings is not None else earnings_report([])

    def release(self, package: str, track: str = "production") -> Release | None:
        if ("release", package) in self._fail:
            raise self._fail[("release", package)]
        return self._releases.get(package)


class FakeApple(tools.AppleGateway):
    def __init__(
        self,
        *,
        configured: bool = True,
        vendor: bool = True,
        apps: list[StoreApp] | None = None,
        sales: Report | None = None,
        releases: dict[str, Release] | None = None,
        fail: dict[tuple[str, str], Exception] | None = None,
    ) -> None:
        self._configured = configured
        self._vendor = vendor
        self._apps = list(apps or [])
        self._sales = sales
        self._releases = releases or {}
        self._fail = dict(fail or {})

    def available(self) -> str | None:
        if not self._configured:
            return (
                "App Store Connect is not configured. Set STOREPILOT_ASC_KEY_PATH (the .p8), "
                "STOREPILOT_ASC_KEY_ID and STOREPILOT_ASC_ISSUER_ID."
            )
        return None

    def sales_available(self) -> str | None:
        if not self._vendor:
            return (
                "STOREPILOT_ASC_VENDOR_NUMBER is not set, so Apple sales reports cannot be read."
            )
        return None

    def list_apps(self) -> list[StoreApp]:
        if ("list_apps", "") in self._fail:
            raise self._fail[("list_apps", "")]
        return self._apps

    def sales(self, month: str) -> Report:
        if ("sales", "") in self._fail:
            raise self._fail[("sales", "")]
        return self._sales if self._sales is not None else sales_report([])

    def release(self, apple_id: str) -> Release | None:
        if ("release", apple_id) in self._fail:
            raise self._fail[("release", apple_id)]
        return self._releases.get(apple_id)
