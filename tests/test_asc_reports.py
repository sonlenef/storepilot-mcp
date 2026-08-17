"""App Store Connect sales and analytics reports.

The headline hazard is the per-unit trap: Apple's "Developer Proceeds" column is
the proceeds for ONE unit, so summing the column under-reports revenue by the
unit count. A popular app's month reads as pocket change and nothing looks wrong.

The second hazard is silence: Apple omits the report entirely for a period with
no transactions, and has not published today's yet — both of which look exactly
like "$0 of sales" unless the freshness caveat says otherwise.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from storepilot.app_store.client import AscClient
from storepilot.app_store.reports import (
    MAX_SALES_FETCHES,
    AnalyticsStage,
    Frequency,
    advance_analytics,
    check_range_size,
    daily_range,
    fetch_sales_report,
    get_sales,
    iter_tsv,
    maybe_gunzip,
    normalize_report_date,
    parse_analytics_segment,
    parse_sales_report,
    sales_freshness,
    vendor_number,
    version_candidates,
)
from storepilot.core.errors import StorePermissionError, ValidationError
from tests.support.asc import (
    apple_error,
    gzipped,
    json_response,
    make_credentials,
    resource,
    routed_transport,
    sequence_transport,
    tsv,
)

VENDOR = "80123456"

SALES_HEADER = [
    "Provider", "Provider Country", "SKU", "Developer", "Title", "Version",
    "Product Type Identifier", "Units", "Developer Proceeds", "Begin Date", "End Date",
    "Customer Currency", "Country Code", "Currency of Proceeds", "Apple Identifier",
    "Customer Price", "Promo Code", "Parent Identifier", "Subscription", "Period",
    "Category", "CMB", "Device", "Supported Platforms", "Proceeds Reason",
    "Preserved Pricing", "Client", "Order Type",
]


def sales_row(
    *,
    units: str,
    proceeds: str,
    apple_id: str = "1234567890",
    country: str = "US",
    currency: str = "USD",
    begin: str = "07/01/2026",
) -> list[str]:
    return [
        "APPLE", "US", "ACME_TODO", "Acme Inc", "Acme Todo", "4.2.0", "1", units, proceeds,
        begin, begin, currency, country, currency, apple_id, "0.99", "", "", "", "",
        "Productivity", "", "iPhone", "iOS", "", "", "", "",
    ]


def sales_tsv(rows: list[list[str]]) -> bytes:
    return tsv([SALES_HEADER, *rows])


def build_client(tmp_path: Path, transport: httpx.MockTransport) -> AscClient:
    return AscClient(
        credentials=make_credentials(tmp_path),
        transport=transport,
        sleep=lambda _s: None,
        max_retries=0,
    )


# --- The per-unit trap -------------------------------------------------------


def test_proceeds_is_units_times_the_per_unit_column() -> None:
    """500 units at $0.70 proceeds is $350, not $0.70."""
    data = sales_tsv([sales_row(units="500", proceeds="0.70")])
    rows = parse_sales_report(data, report_date="2026-07-01")

    by_metric = {row.metric: row.value for row in rows}
    assert by_metric["units"] == 500.0
    assert by_metric["proceeds_per_unit"] == 0.70
    assert by_metric["proceeds"] == pytest.approx(350.0)


def test_summing_the_proceeds_column_would_understate_revenue_by_orders_of_magnitude() -> None:
    data = sales_tsv(
        [
            sales_row(units="500", proceeds="0.70"),
            sales_row(units="1200", proceeds="1.40", country="GB"),
            sales_row(units="3", proceeds="6.99", country="DE"),
        ]
    )
    rows = parse_sales_report(data, report_date="2026-07-01")

    real = sum(r.value for r in rows if r.metric == "proceeds")
    naive_column_sum = sum(r.value for r in rows if r.metric == "proceeds_per_unit")

    assert real == pytest.approx(350.0 + 1680.0 + 20.97)
    assert naive_column_sum == pytest.approx(9.09)
    assert real > naive_column_sum * 100


def test_refunds_stay_negative_so_the_total_is_net() -> None:
    data = sales_tsv(
        [
            sales_row(units="500", proceeds="0.70"),
            sales_row(units="-20", proceeds="0.70"),
        ]
    )
    rows = parse_sales_report(data, report_date="2026-07-01")
    assert sum(r.value for r in rows if r.metric == "proceeds") == pytest.approx(336.0)
    assert sum(r.value for r in rows if r.metric == "units") == 480.0


def test_money_rows_carry_a_currency_and_unit_rows_do_not() -> None:
    data = sales_tsv([sales_row(units="10", proceeds="1.00", currency="EUR", country="DE")])
    rows = parse_sales_report(data, report_date="2026-07-01")
    by_metric = {row.metric: row for row in rows}

    assert by_metric["proceeds"].currency == "EUR"
    assert by_metric["proceeds_per_unit"].currency == "EUR"
    assert by_metric["units"].currency is None, "a unit count has no currency"
    assert by_metric["proceeds"].dimension == "country"
    assert by_metric["proceeds"].dimension_value == "DE"
    assert by_metric["proceeds"].period == date(2026, 7, 1)


def test_rows_can_be_filtered_to_one_app() -> None:
    data = sales_tsv(
        [
            sales_row(units="10", proceeds="1.00", apple_id="1234567890"),
            sales_row(units="99", proceeds="9.00", apple_id="1999888777"),
        ]
    )
    rows = parse_sales_report(data, report_date="2026-07-01", app_id="1234567890")
    assert {r.app_id for r in rows} == {"1234567890"}
    assert sum(r.value for r in rows if r.metric == "units") == 10.0


def test_missing_numbers_are_skipped_rather_than_read_as_zero() -> None:
    data = sales_tsv([sales_row(units="", proceeds="")])
    assert parse_sales_report(data, report_date="2026-07-01") == []


# --- Transport formats -------------------------------------------------------


def test_gzip_is_sniffed_not_assumed() -> None:
    plain = sales_tsv([sales_row(units="1", proceeds="1.00")])
    assert maybe_gunzip(plain) == plain
    assert maybe_gunzip(gzipped(plain)) == plain


def test_a_truncated_gzip_is_reported_as_such() -> None:
    broken = gzipped(b"hello world")[:8]
    with pytest.raises(ValidationError) as excinfo:
        maybe_gunzip(broken)
    assert "truncated gzip" in excinfo.value.message
    assert "clear the StorePilot cache" in excinfo.value.remedy


def test_tsv_reader_keeps_apples_exact_headers_and_skips_blank_rows() -> None:
    data = tsv([["Units", "Developer Proceeds"], ["5", "1.00"], ["", ""], ["6", "2.00"]])
    rows = list(iter_tsv(data))
    assert rows == [
        {"Units": "5", "Developer Proceeds": "1.00"},
        {"Units": "6", "Developer Proceeds": "2.00"},
    ]


# --- reportDate validation ---------------------------------------------------


def test_weekly_reports_are_keyed_by_the_sunday_that_ends_the_week() -> None:
    assert normalize_report_date("2026-07-05", Frequency.WEEKLY) == "2026-07-05"  # a Sunday
    with pytest.raises(ValidationError) as excinfo:
        normalize_report_date("2026-07-06", Frequency.WEEKLY)  # a Monday
    assert "2026-07-12" in excinfo.value.remedy


@pytest.mark.parametrize(
    ("raw", "frequency", "expected"),
    [
        ("2026-07-15", Frequency.DAILY, "2026-07-15"),
        ("2026-07", Frequency.MONTHLY, "2026-07"),
        ("2026-07-15", Frequency.MONTHLY, "2026-07"),
        ("2026", Frequency.YEARLY, "2026"),
    ],
)
def test_report_date_granularity(raw: str, frequency: Frequency, expected: str) -> None:
    assert normalize_report_date(raw, frequency) == expected


@pytest.mark.parametrize(
    ("raw", "frequency"),
    [
        ("2026-07", Frequency.DAILY),
        ("July 2026", Frequency.MONTHLY),
        ("2026-07", Frequency.YEARLY),
    ],
)
def test_bad_report_dates_are_caught_locally(raw: str, frequency: Frequency) -> None:
    """Apple's 400 for a mismatched format never mentions the format."""
    with pytest.raises(ValidationError):
        normalize_report_date(raw, frequency)


# --- Freshness ---------------------------------------------------------------


def test_a_day_apple_has_not_published_yet_is_not_zero_sales() -> None:
    freshness = sales_freshness("2026-07-15", Frequency.DAILY, today=date(2026, 7, 15))
    assert freshness.is_complete is False
    assert "not published" in (freshness.caveat or "")
    assert "not evidence of zero sales" in (freshness.caveat or "")


def test_a_published_day_with_rows_is_complete() -> None:
    freshness = sales_freshness(
        "2026-07-15", Frequency.DAILY, today=date(2026, 7, 20), row_count=12
    )
    assert freshness.is_complete is True
    assert freshness.caveat is None


def test_a_missing_report_says_it_might_be_the_vendor_number() -> None:
    freshness = sales_freshness(
        "2026-07-15", Frequency.DAILY, today=date(2026, 7, 20), missing=True
    )
    assert freshness.is_complete is False
    assert "STOREPILOT_ASC_VENDOR_NUMBER" in (freshness.caveat or "")


def test_monthly_freshness_waits_for_the_month_to_end_plus_five_days() -> None:
    assert (
        sales_freshness("2026-07", Frequency.MONTHLY, today=date(2026, 8, 3)).is_complete is False
    )
    assert (
        sales_freshness(
            "2026-07", Frequency.MONTHLY, today=date(2026, 8, 6), row_count=3
        ).is_complete
        is True
    )


# --- Quota protection --------------------------------------------------------


def test_a_range_that_would_burn_the_sales_quota_is_refused_before_the_first_call() -> None:
    with pytest.raises(ValidationError) as excinfo:
        check_range_size(90)
    assert excinfo.value.details["cap"] == MAX_SALES_FETCHES
    assert "frequency='MONTHLY'" in excinfo.value.remedy
    check_range_size(MAX_SALES_FETCHES)  # exactly at the cap is fine


def test_daily_range_rejects_a_backwards_range() -> None:
    assert daily_range(date(2026, 7, 1), date(2026, 7, 3)) == [
        date(2026, 7, 1),
        date(2026, 7, 2),
        date(2026, 7, 3),
    ]
    with pytest.raises(ValidationError, match="before start date"):
        daily_range(date(2026, 7, 3), date(2026, 7, 1))


def test_a_missing_vendor_number_names_where_to_find_it() -> None:
    with pytest.raises(ValidationError) as excinfo:
        vendor_number()
    assert "Payments and Financial Reports" in excinfo.value.remedy


def test_version_candidates_are_ordered_per_report_type() -> None:
    assert version_candidates("SALES", "SUMMARY")[0] == "1_1"
    assert version_candidates("SUBSCRIBER", "DETAILED")[0] == "1_3"
    assert version_candidates("MADE_UP", "SUMMARY") == ("1_0", None)


# --- Fetching ----------------------------------------------------------------


def test_a_rejected_version_falls_through_to_the_next_candidate(tmp_path: Path) -> None:
    """Apple answers a bad type/version combination with a bare 400."""
    log: list[httpx.Request] = []
    payload = sales_tsv([sales_row(units="5", proceeds="1.00")])
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        version = request.url.params.get("filter[version]")
        attempts.append(version or "<omitted>")
        if version == "1_1":
            return apple_error(400, code="PARAMETER_ERROR.INVALID", detail="version not valid")
        return httpx.Response(200, content=payload)

    client = build_client(tmp_path, httpx.MockTransport(handler))
    data, used = fetch_sales_report(
        client, report_date="2026-07-01", vendor=VENDOR, today=date(2026, 8, 1)
    )

    assert attempts == ["1_1", "1_0"]
    assert used == "1_0"
    assert data == payload


def test_a_404_means_apple_has_no_report_not_that_something_broke(tmp_path: Path) -> None:
    client = build_client(
        tmp_path, sequence_transport([apple_error(404, code="NOT_FOUND", detail="no report")])
    )
    data, _ = fetch_sales_report(
        client, report_date="2026-07-01", vendor=VENDOR, today=date(2026, 8, 1)
    )
    assert data is None


def test_a_past_period_is_fetched_once_and_then_served_from_cache(tmp_path: Path) -> None:
    log: list[httpx.Request] = []
    payload = sales_tsv([sales_row(units="5", proceeds="1.00")])
    client = build_client(
        tmp_path, routed_transport({"/v1/salesReports": httpx.Response(200, content=payload)}, log=log)
    )

    for _ in range(3):
        data, _ = fetch_sales_report(
            client, report_date="2026-07-01", vendor=VENDOR, today=date(2026, 8, 1)
        )
        assert data == payload

    assert len(log) == 1, "a past period never changes; re-fetching burns scarce quota"


def test_an_empty_period_is_cached_too(tmp_path: Path) -> None:
    log: list[httpx.Request] = []
    client = build_client(
        tmp_path,
        routed_transport({"/v1/salesReports": apple_error(404, code="NOT_FOUND")}, log=log),
    )
    for _ in range(3):
        assert fetch_sales_report(
            client, report_date="2026-07-01", vendor=VENDOR, today=date(2026, 8, 1)
        )[0] is None
    assert len(log) == 1


def test_a_403_on_sales_names_the_roles_that_can_read_them(tmp_path: Path) -> None:
    client = build_client(
        tmp_path, sequence_transport([apple_error(403, detail="Forbidden")])
    )
    with pytest.raises(StorePermissionError) as excinfo:
        fetch_sales_report(client, report_date="2026-07-01", vendor=VENDOR, today=date(2026, 8, 1))
    assert "Admin, Finance, or Sales" in excinfo.value.remedy


def test_every_version_rejected_produces_one_actionable_error(tmp_path: Path) -> None:
    client = build_client(
        tmp_path,
        routed_transport(
            {"/v1/salesReports": apple_error(400, detail="Invalid combination")},
        ),
    )
    with pytest.raises(ValidationError) as excinfo:
        fetch_sales_report(
            client, report_date="2026-07-01", vendor=VENDOR, today=date(2026, 8, 1)
        )
    assert excinfo.value.details["versions_attempted"] == ["1_1", "1_0", "<omitted>"]
    assert "Invalid combination" in str(excinfo.value.details["apple_detail"])


def test_get_sales_returns_rows_with_their_freshness(tmp_path: Path) -> None:
    payload = sales_tsv(
        [
            sales_row(units="500", proceeds="0.70"),
            sales_row(units="100", proceeds="0.70", country="GB"),
        ]
    )
    client = build_client(
        tmp_path, routed_transport({"/v1/salesReports": httpx.Response(200, content=payload)})
    )
    report = get_sales(
        client, report_date="2026-07-01", vendor=VENDOR, today=date(2026, 8, 1)
    )

    assert report.total("proceeds") == pytest.approx(420.0)
    assert report.freshness.is_complete is True
    assert "salesReports:SALES/SUMMARY/DAILY/2026-07-01" in (report.source_object or "")


def test_get_sales_for_an_empty_period_reports_the_ambiguity(tmp_path: Path) -> None:
    client = build_client(
        tmp_path, routed_transport({"/v1/salesReports": apple_error(404, code="NOT_FOUND")})
    )
    report = get_sales(client, report_date="2026-07-01", vendor=VENDOR, today=date(2026, 8, 1))
    assert report.rows == []
    assert report.freshness.is_complete is False
    assert report.freshness.warning() is not None


# --- Analytics: the asynchronous chain ---------------------------------------


def test_no_analytics_request_yet_tells_the_caller_to_create_one(tmp_path: Path) -> None:
    client = build_client(
        tmp_path,
        routed_transport({"/v1/apps/1/analyticsReportRequests": json_response({"data": []})}),
    )
    progress = advance_analytics(client, "1", create=False)
    assert progress.stage is AnalyticsStage.NOT_REQUESTED
    assert "create=true" in progress.next_action


def test_a_fresh_request_says_to_come_back_tomorrow_not_to_re_request(tmp_path: Path) -> None:
    routes = {
        "/v1/apps/1/analyticsReportRequests": json_response({"data": []}),
        "/v1/analyticsReportRequests": json_response(
            {"data": resource("analyticsReportRequests", "req1", accessType="ONGOING")}
        ),
    }
    client = build_client(tmp_path, routed_transport(routes))
    progress = advance_analytics(client, "1", create=True)

    assert progress.stage is AnalyticsStage.PROVISIONING
    assert progress.request_id == "req1"
    assert "24-48 hours" in progress.next_action
    assert "would only reset the clock" in progress.next_action


def test_a_stopped_request_is_reported_as_stopped(tmp_path: Path) -> None:
    client = build_client(
        tmp_path,
        routed_transport(
            {
                "/v1/apps/1/analyticsReportRequests": json_response(
                    {
                        "data": [
                            resource(
                                "analyticsReportRequests",
                                "old",
                                accessType="ONGOING",
                                stoppedDueToInactivity=True,
                            )
                        ]
                    }
                )
            }
        ),
    )
    progress = advance_analytics(client, "1", create=False)
    assert progress.stage is AnalyticsStage.STOPPED
    assert "stoppedDueToInactivity" in progress.next_action


def test_the_full_chain_downloads_and_parses_the_newest_instance(tmp_path: Path) -> None:
    segment_tsv = tsv(
        [
            ["Date", "App Name", "Territory", "Impressions", "Page Views"],
            ["2026-07-30", "Acme Todo", "US", "1200", "300"],
            ["2026-07-30", "Acme Todo", "VN", "400", "90"],
        ]
    )
    routes = {
        "/v1/apps/1/analyticsReportRequests": json_response(
            {"data": [resource("analyticsReportRequests", "req1", accessType="ONGOING")]}
        ),
        "/v1/analyticsReportRequests/req1/reports": json_response(
            {"data": [resource("analyticsReports", "rep1", name="App Store Engagement")]}
        ),
        "/v1/analyticsReports/rep1/instances": json_response(
            {
                "data": [
                    resource("analyticsReportInstances", "old", processingDate="2026-07-29"),
                    resource("analyticsReportInstances", "new", processingDate="2026-07-30"),
                ]
            }
        ),
        "/v1/analyticsReportInstances/new/segments": json_response(
            {
                "data": [
                    resource(
                        "analyticsReportSegments",
                        "seg1",
                        url="https://cdn.apple.com/segments/seg1",
                        checksum="chk1",
                        sizeInBytes=len(segment_tsv),
                    )
                ]
            }
        ),
        "/segments/": httpx.Response(200, content=gzipped(segment_tsv)),
    }
    client = build_client(tmp_path, routed_transport(routes))

    progress = advance_analytics(client, "1", category="APP_STORE_ENGAGEMENT")

    assert progress.stage is AnalyticsStage.DATA_READY
    assert progress.instance_id == "new", "the newest processingDate wins"
    assert progress.report is not None
    assert progress.report.total("impressions") == 1600.0
    assert progress.report.total("page_views") == 390.0
    # Dimension resolution is positional: the first known dimension column in the
    # row wins, which here is "App Name" rather than "Territory". Totals are
    # unaffected; only the label is.
    assert {row.dimension for row in progress.report.rows} == {"app_name"}
    assert "Segments: 1" in progress.render()


def test_analytics_segments_parse_structurally_not_against_a_fixed_schema() -> None:
    data = tsv(
        [
            ["Date", "Territory", "A Brand New Metric"],
            ["2026-07-30", "US", "42"],
        ]
    )
    rows = parse_analytics_segment(data, app_id="1")
    assert [(r.metric, r.value) for r in rows] == [("a_brand_new_metric", 42.0)]
    assert rows[0].dimension == "territory"
    assert rows[0].period == date(2026, 7, 30)


def test_unknown_analytics_arguments_are_refused_with_the_valid_set(tmp_path: Path) -> None:
    client = build_client(tmp_path, routed_transport({}))
    with pytest.raises(ValidationError, match="Unknown analytics category"):
        advance_analytics(client, "1", category="NOPE")
    with pytest.raises(ValidationError, match="Unknown analytics granularity"):
        advance_analytics(client, "1", granularity="HOURLY")
