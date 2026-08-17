"""Google Play report CSVs — the place a wrong answer costs the user money.

These files are UTF-16 with a BOM, Google renames columns without notice, and
every metric here is either money or the number the user judges the app by. The
failure mode is not a crash, it is a confident wrong number: "Uninstall events"
read as installs, buyer currency read as payout, or an unpublished month read as
a zero-revenue month.

Fixtures under ``tests/fixtures/`` are real-shaped: real column names, real
column order, thousands separators, blank rows and "NA" cells included.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from storepilot.core.csv_reports import (
    ReportKind,
    decode_report_bytes,
    find_column,
    iter_report_rows,
    month_bounds,
    normalize_header,
    normalize_month,
    parse_crashes,
    parse_date,
    parse_earnings,
    parse_float,
    parse_installs,
    parse_ratings,
    report_freshness,
    report_object_path,
    report_object_prefix,
    summarize,
)
from storepilot.core.errors import ValidationError


@pytest.fixture
def installs_csv(fixture_dir: Path) -> bytes:
    return (fixture_dir / "installs_overview_utf16.csv").read_bytes()


# --- Decoding ----------------------------------------------------------------


def test_fixture_really_is_utf16_with_a_bom(installs_csv: bytes) -> None:
    """Guards the fixture itself: a UTF-8 fixture would prove nothing."""
    assert installs_csv[:2] == b"\xff\xfe"
    with pytest.raises(UnicodeDecodeError):
        installs_csv.decode("utf-8")


def test_decodes_utf16_le_with_bom(installs_csv: bytes) -> None:
    text = decode_report_bytes(installs_csv)
    assert text.startswith('"Date","Package Name"')
    assert "\x00" not in text


def test_decodes_utf8_bom_and_bomless_utf16be(fixture_dir: Path) -> None:
    for name in ("installs_overview_utf8_bom.csv", "installs_overview_utf16be.csv"):
        text = decode_report_bytes((fixture_dir / name).read_bytes())
        assert "Daily Device Installs" in text, name
        assert "\x00" not in text, name


def test_undecodable_bytes_raise_an_actionable_error() -> None:
    with pytest.raises(ValidationError) as excinfo:
        decode_report_bytes(b"\xc3\x28\xa0\xa1" * 40)
    assert "neither valid UTF-16 nor UTF-8" in excinfo.value.message
    assert "Re-download" in excinfo.value.remedy


# --- Header handling ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Daily Device Installs", "daily_device_installs"),
        ("  Total User Installs  ", "total_user_installs"),
        ("Amount (Merchant Currency)", "amount_merchant_currency"),
        ("Country / Region", "country_region"),
        ("Android OS Version", "android_os_version"),
    ],
)
def test_normalize_header(raw: str, expected: str) -> None:
    assert normalize_header(raw) == expected


def test_rows_are_keyed_by_normalized_header(installs_csv: bytes) -> None:
    rows = list(iter_report_rows(installs_csv))
    assert rows[0]["date"] == "2026-07-01"
    assert rows[0]["daily_device_installs"] == "120"
    assert rows[0]["install_events"] == "131"
    # The fixture's fourth row is a day whose metric cells are all empty — a real
    # occurrence, and not the same thing as a zero.
    assert len(rows) == 4
    assert rows[3]["daily_device_installs"] == ""


def test_blank_lines_are_skipped() -> None:
    text = '"Date","Daily Device Installs"\n"2026-07-01","120"\n"",""\n\n"2026-07-02","98"\n'
    assert [row["daily_device_installs"] for row in iter_report_rows(text)] == ["120", "98"]


def test_a_renamed_column_still_resolves(fixture_dir: Path) -> None:
    """Google appending a suffix must not lose the metric."""
    data = (fixture_dir / "installs_renamed_column_utf16.csv").read_bytes()
    report = parse_installs(data, package_name="com.acme.todo", month="2026-07")
    assert summarize(report.rows)["daily_device_installs"] == 120


# --- The near-miss trap ------------------------------------------------------


def test_install_events_never_resolves_to_uninstall_events() -> None:
    """Substring matching would report uninstalls as installs. Whole tokens only."""
    row = {"date": "2026-07-01", "uninstall_events": "29"}
    with pytest.raises(ValidationError) as excinfo:
        find_column(row, ("Install events",), source="installs.csv")
    assert "available_headers" in excinfo.value.details
    assert find_column(row, ("Install events",), required=False) is None


def test_uninstall_events_never_resolves_to_install_events() -> None:
    row = {"date": "2026-07-01", "install_events": "131"}
    assert find_column(row, ("Uninstall events",), required=False) is None


def test_uninstall_only_report_reports_no_installs(fixture_dir: Path) -> None:
    """End to end: a report carrying only uninstalls must never yield installs."""
    data = (fixture_dir / "installs_uninstall_only_utf16.csv").read_bytes()
    totals = summarize(parse_installs(data, package_name="com.acme.todo", month="2026-07").rows)
    assert totals == {"uninstall_events": 29.0}
    assert "install_events" not in totals


def test_single_token_candidates_must_match_exactly() -> None:
    """'Country' must not silently resolve to 'Buyer Country' — different columns."""
    row = {"date": "2026-07-01", "buyer_country": "US"}
    assert find_column(row, ("Country",), required=False) is None
    assert find_column(row, ("Country", "Buyer Country"), required=False) == "buyer_country"


def test_date_does_not_resolve_to_update_events() -> None:
    row = {"update_events": "402", "daily_device_installs": "120"}
    assert find_column(row, ("Date",), required=False) is None


# --- Missing columns are loud ------------------------------------------------


def test_a_report_with_no_known_metric_raises_and_lists_the_headers(fixture_dir: Path) -> None:
    data = (fixture_dir / "installs_no_known_metrics_utf16.csv").read_bytes()
    with pytest.raises(ValidationError) as excinfo:
        parse_installs(data, package_name="com.acme.todo", month="2026-07")

    error = excinfo.value
    assert "Google changes report columns" in error.message
    assert "some_brand_new_metric" in error.details["available_headers"]
    assert "another_column" in error.details["available_headers"]
    assert error.doc_url


def test_missing_earnings_amount_column_raises_rather_than_reporting_zero() -> None:
    """Silence here would look exactly like "the app earned nothing"."""
    csv_text = '"Transaction Date","Product id","Merchant Currency"\n"Jul 1, 2026","com.acme.todo","USD"\n'
    with pytest.raises(ValidationError) as excinfo:
        parse_earnings(csv_text, month="2026-07")
    assert "Amount (Merchant Currency)" in excinfo.value.message


# --- Installs ----------------------------------------------------------------


def test_parse_installs_totals(installs_csv: bytes) -> None:
    report = parse_installs(
        installs_csv, package_name="com.acme.todo", month="2026-07", today=date(2026, 8, 20)
    )
    totals = summarize(report.rows)
    assert totals["daily_device_installs"] == 120 + 98 + 1240  # thousands separator parsed
    assert totals["daily_device_uninstalls"] == 30 + 22 + 44
    assert totals["install_events"] == 131 + 104 + 1301
    assert totals["uninstall_events"] == 29 + 21 + 43
    assert report.source_object == "stats/installs/installs_com.acme.todo_202607_overview.csv"
    assert {row.app_id for row in report.rows} == {"com.acme.todo"}
    assert {row.period for row in report.rows} == {
        date(2026, 7, 1),
        date(2026, 7, 2),
        date(2026, 7, 3),
    }


def test_dimension_rows_carry_their_dimension(fixture_dir: Path) -> None:
    data = (fixture_dir / "installs_country_utf16.csv").read_bytes()
    report = parse_installs(
        data, package_name="com.acme.todo", month="2026-07", dimension="country"
    )
    installs = [r for r in report.rows if r.metric == "daily_device_installs"]
    assert {(r.dimension, r.dimension_value, r.value) for r in installs} == {
        ("country", "US", 80.0),
        ("country", "VN", 40.0),
    }


def test_parse_ratings_and_crashes(fixture_dir: Path) -> None:
    ratings = parse_ratings(
        (fixture_dir / "ratings_overview_utf16.csv").read_bytes(),
        package_name="com.acme.todo",
        month="2026-07",
    )
    latest = max(
        (r for r in ratings.rows if r.metric == "total_average_rating"), key=lambda r: r.period
    )
    assert latest.value == 4.31
    # "NA" is absence, not a zero-star day.
    assert [r.value for r in ratings.rows if r.metric == "daily_average_rating"] == [4.5, 3.0]

    crashes = parse_crashes(
        (fixture_dir / "crashes_overview_utf16.csv").read_bytes(),
        package_name="com.acme.todo",
        month="2026-07",
    )
    assert summarize(crashes.rows) == {"daily_crashes": 21.0, "daily_anrs": 4.0}


# --- Earnings ----------------------------------------------------------------


def test_earnings_reads_the_merchant_amount_not_the_buyer_amount(fixture_dir: Path) -> None:
    """The buyer paid 9.99; the developer received 6.99. Only one is revenue."""
    data = (fixture_dir / "earnings_utf16.csv").read_bytes()
    report = parse_earnings(data, month="2026-07", today=date(2026, 8, 20))

    todo_app = [r for r in report.rows if r.app_id == "com.acme.todo"]
    assert [r.value for r in todo_app] == [6.99, -1.40]
    assert 9.99 not in [r.value for r in report.rows]


def test_earnings_carries_currency_per_row_and_never_assumes_usd(fixture_dir: Path) -> None:
    data = (fixture_dir / "earnings_utf16.csv").read_bytes()
    report = parse_earnings(data, month="2026-07", today=date(2026, 8, 20))
    by_currency: dict[str, float] = {}
    for row in report.rows:
        by_currency[row.currency or "?"] = by_currency.get(row.currency or "?", 0.0) + row.value

    assert by_currency["USD"] == pytest.approx(6.99 + 3.49 - 1.40)
    assert by_currency["VND"] == pytest.approx(174_300.0)  # thousands separators survive
    assert by_currency["EUR"] == pytest.approx(2.09)
    assert None not in by_currency


def test_earnings_filtered_to_one_package_includes_its_in_app_products(
    fixture_dir: Path,
) -> None:
    data = (fixture_dir / "earnings_utf16.csv").read_bytes()
    report = parse_earnings(data, month="2026-07", package_name="com.acme.todo")
    assert report.total("earnings") == pytest.approx(6.99 + 3.49 - 1.40)
    assert {r.app_id for r in report.rows} == {"com.acme.todo"}


def test_earnings_dates_parse_the_human_format(fixture_dir: Path) -> None:
    data = (fixture_dir / "earnings_utf16.csv").read_bytes()
    report = parse_earnings(data, month="2026-07")
    assert min(r.period for r in report.rows) == date(2026, 7, 1)
    assert max(r.period for r in report.rows) == date(2026, 7, 5)


# --- Freshness: an unpublished month is not a zero month ---------------------


def test_earnings_for_a_month_google_has_not_published_is_flagged() -> None:
    freshness = report_freshness(ReportKind.EARNINGS, "2026-07", today=date(2026, 8, 1))
    assert freshness.is_complete is False
    assert freshness.lag_days == 4
    assert "not published yet" in (freshness.caveat or "")
    assert "not because revenue was zero" in (freshness.caveat or "")
    assert freshness.warning() is not None


def test_earnings_after_the_publish_date_are_complete() -> None:
    freshness = report_freshness(ReportKind.EARNINGS, "2026-07", today=date(2026, 8, 6))
    assert freshness.is_complete is True
    assert freshness.caveat is None
    assert freshness.warning() is None


def test_stats_for_the_month_in_progress_are_flagged_as_still_filling_in() -> None:
    freshness = report_freshness(ReportKind.INSTALLS, "2026-07", today=date(2026, 7, 20))
    assert freshness.is_complete is False
    assert "still filling in" in (freshness.caveat or "")
    assert freshness.as_of == date(2026, 7, 17)  # three days behind, not "today"


def test_stats_with_no_rows_after_the_window_closed_says_which_it_is() -> None:
    freshness = report_freshness(
        ReportKind.INSTALLS, "2026-07", today=date(2026, 9, 1), row_count=0
    )
    assert freshness.is_complete is False
    assert "report object is missing" in (freshness.caveat or "")


def test_stats_with_rows_after_the_window_closed_are_complete() -> None:
    freshness = report_freshness(
        ReportKind.INSTALLS, "2026-07", today=date(2026, 9, 1), row_count=42
    )
    assert freshness.is_complete is True
    assert freshness.as_of == date(2026, 7, 31)


def test_installs_report_for_an_unfinished_month_carries_the_caveat(
    installs_csv: bytes,
) -> None:
    report = parse_installs(
        installs_csv, package_name="com.acme.todo", month="2026-07", today=date(2026, 7, 10)
    )
    assert report.rows, "rows are still returned; only the confidence changes"
    assert report.freshness.is_stale is True


# --- Small parsers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1,240", 1240.0), ("4.31", 4.31), ("-1.40", -1.40), ("12%", 12.0), ("", None),
     ("NA", None), ("N/A", None), ("-", None), ("garbage", None), (None, None)],
)
def test_parse_float(raw: str | None, expected: float | None) -> None:
    assert parse_float(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-01", date(2026, 7, 1)),
        ("Jul 1, 2026", date(2026, 7, 1)),
        ("July 1, 2026", date(2026, 7, 1)),
        ("07/01/2026", date(2026, 7, 1)),
        ("20260701", date(2026, 7, 1)),
        ("not a date", None),
        ("", None),
    ],
)
def test_parse_date(raw: str, expected: date | None) -> None:
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["2026-07", "202607", "2026/07"])
def test_normalize_month_accepts_the_forms_people_type(raw: str) -> None:
    assert normalize_month(raw) == "202607"


@pytest.mark.parametrize("raw", ["2026", "2026-13", "july", "20260"])
def test_normalize_month_rejects_the_rest(raw: str) -> None:
    with pytest.raises(ValidationError):
        normalize_month(raw)


def test_month_bounds_handles_december_rollover() -> None:
    assert month_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))
    assert month_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))


# --- Object paths ------------------------------------------------------------


def test_report_object_paths() -> None:
    assert (
        report_object_path(ReportKind.INSTALLS, "2026-07", package_name="com.acme.todo")
        == "stats/installs/installs_com.acme.todo_202607_overview.csv"
    )
    assert report_object_path(ReportKind.SALES, "2026-07") == "sales/salesreport_202607.csv"
    assert report_object_prefix(ReportKind.EARNINGS, "2026-07") == "earnings/earnings_202607"


def test_earnings_object_name_is_not_derivable_and_says_so() -> None:
    with pytest.raises(ValidationError) as excinfo:
        report_object_path(ReportKind.EARNINGS, "2026-07")
    assert "report_object_prefix" in excinfo.value.remedy


def test_per_app_report_without_a_package_is_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        report_object_path(ReportKind.INSTALLS, "2026-07")
    assert "no package_name was given" in excinfo.value.message
