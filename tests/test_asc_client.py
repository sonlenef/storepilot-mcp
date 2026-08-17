"""The App Store Connect HTTP client.

Everything here is exercised through ``httpx.MockTransport``; the clock, the
sleep and the retry count are injected, so a test that proves the client backs
off for 7 seconds takes microseconds and needs no Apple account.

The properties that matter: a paginated collection is walked to the end (a
half-walked list silently under-reports), a truncated one says so, Apple's rate
limits are respected before they are hit, and every HTTP failure becomes the
error type whose remedy actually fixes it.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from storepilot.app_store.client import (
    HOUR_CRITICAL,
    HOUR_LOW_WATER,
    AscClient,
    RateLimitSnapshot,
    Throttle,
    attrs,
    classify_asc_error,
    index_included,
    parse_rate_limit,
    parse_retry_after,
    resolve_related,
    resource_body,
)
from storepilot.core.errors import (
    CredentialsError,
    NotFoundError,
    RateLimitError,
    StorePermissionError,
    UpstreamError,
    ValidationError,
)
from tests.support.asc import (
    apple_error,
    json_response,
    make_credentials,
    resource,
    routed_transport,
    sequence_transport,
)


@pytest.fixture
def sleeps() -> list[float]:
    return []


def build_client(
    tmp_path: Path,
    transport: httpx.MockTransport,
    *,
    sleeps: list[float] | None = None,
    max_retries: int = 3,
) -> AscClient:
    return AscClient(
        credentials=make_credentials(tmp_path),
        transport=transport,
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
        max_retries=max_retries,
    )


# --- Auth injection ----------------------------------------------------------


def test_every_request_carries_a_bearer_token(tmp_path: Path) -> None:
    log: list[httpx.Request] = []
    client = build_client(
        tmp_path, routed_transport({"/v1": json_response({"data": []})}, log=log)
    )
    client.get_json("/v1/apps")

    assert log[0].headers["authorization"].startswith("Bearer ey")
    assert log[0].headers["accept"] == "application/json"


def test_a_401_reminting_happens_once_and_only_once(tmp_path: Path, sleeps: list[float]) -> None:
    """A token can expire in flight; a second 401 is a real credential problem."""
    log: list[httpx.Request] = []
    transport = sequence_transport(
        [
            apple_error(401, code="NOT_AUTHORIZED", detail="token expired"),
            json_response({"data": [resource("apps", "1")]}),
        ],
        log=log,
    )
    client = build_client(tmp_path, transport, sleeps=sleeps)
    assert client.get_json("/v1/apps")["data"]

    assert len(log) == 2
    assert log[0].headers["authorization"] != log[1].headers["authorization"]

    persistent = build_client(
        tmp_path,
        sequence_transport([apple_error(401, code="NOT_AUTHORIZED", detail="revoked")]),
        sleeps=sleeps,
    )
    with pytest.raises(CredentialsError):
        persistent.get_json("/v1/apps")


# --- Pagination --------------------------------------------------------------


def test_get_all_walks_links_next_to_the_end(tmp_path: Path) -> None:
    log: list[httpx.Request] = []
    page_one = json_response(
        {
            "data": [resource("apps", "1"), resource("apps", "2")],
            "links": {"next": "https://api.appstoreconnect.apple.com/v1/apps?cursor=B"},
            "meta": {"paging": {"total": 5}},
        }
    )
    page_two = json_response(
        {
            "data": [resource("apps", "3"), resource("apps", "4")],
            "links": {"next": "https://api.appstoreconnect.apple.com/v1/apps?cursor=C"},
            "meta": {"paging": {"total": 5}},
        }
    )
    page_three = json_response(
        {"data": [resource("apps", "5")], "meta": {"paging": {"total": 5}}}
    )
    client = build_client(
        tmp_path, sequence_transport([page_one, page_two, page_three], log=log)
    )

    result = client.get_all("/v1/apps")

    assert [item["id"] for item in result.data] == ["1", "2", "3", "4", "5"]
    assert result.pages == 3
    assert result.total == 5
    assert result.truncated is False
    assert result.truncation_note(what="apps") is None
    # The cursor URL carries every original filter; re-sending ours would duplicate them.
    assert log[1].url.params["cursor"] == "B"
    assert "limit" not in log[1].url.params


def test_page_cap_marks_the_result_truncated(tmp_path: Path) -> None:
    endless = json_response(
        {
            "data": [resource("apps", "1")],
            "links": {"next": "https://api.appstoreconnect.apple.com/v1/apps?cursor=X"},
            "meta": {"paging": {"total": 900}},
        }
    )
    client = build_client(tmp_path, sequence_transport([endless]))

    result = client.get_all("/v1/apps", max_pages=3)

    assert result.pages == 3
    assert result.truncated is True
    note = result.truncation_note(what="reviews")
    assert note is not None and "more exist" in note


def test_caller_limit_stops_the_walk_and_trims(tmp_path: Path) -> None:
    page = json_response(
        {
            "data": [resource("apps", str(i)) for i in range(5)],
            "links": {"next": "https://api.appstoreconnect.apple.com/v1/apps?cursor=X"},
        }
    )
    client = build_client(tmp_path, sequence_transport([page]))

    result = client.get_all("/v1/apps", limit=3)

    assert len(result.data) == 3
    assert result.pages == 1
    assert result.truncated is True


def test_a_single_object_response_is_treated_as_one_row(tmp_path: Path) -> None:
    client = build_client(
        tmp_path, sequence_transport([json_response({"data": resource("apps", "42")})])
    )
    page = client.get_page("/v1/apps/42")
    assert [item["id"] for item in page.data] == ["42"]


# --- included resolution -----------------------------------------------------


def test_included_resources_are_indexed_and_resolved(tmp_path: Path) -> None:
    payload = {
        "data": [
            {
                "type": "builds",
                "id": "b1",
                "attributes": {"version": "1180"},
                "relationships": {
                    "app": {"data": {"type": "apps", "id": "1234567890"}},
                    "betaGroups": {
                        "data": [
                            {"type": "betaGroups", "id": "g1"},
                            {"type": "betaGroups", "id": "missing"},
                        ]
                    },
                },
            }
        ],
        "included": [
            resource("apps", "1234567890", name="Acme Todo"),
            resource("betaGroups", "g1", name="Internal"),
        ],
    }
    client = build_client(tmp_path, sequence_transport([json_response(payload)]))
    result = client.get_all("/v1/builds")

    build = result.data[0]
    app = result.related_one(build, "app")
    assert app is not None and attrs(app)["name"] == "Acme Todo"

    groups = result.related(build, "betaGroups")
    assert [attrs(g)["name"] for g in groups] == ["Internal"], (
        "a pointer whose target was not sideloaded is skipped, not invented"
    )
    assert result.related(build, "notARelationship") == []


def test_index_and_resolve_are_pure_functions() -> None:
    payload = {"included": [resource("apps", "1", name="A"), {"type": "apps"}, "junk"]}
    index = index_included(payload)
    assert list(index) == [("apps", "1")]
    assert resolve_related({}, "app", index) == []
    assert attrs(None) == {}
    assert attrs({"attributes": "not a dict"}) == {}


# --- Rate limiting -----------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "limit", "remaining"),
    [
        ("user-hour-lim:3600;user-hour-rem:3121;", 3600, 3121),
        ("user-hour-rem:12", None, 12),
        ("garbage", None, None),
        ("user-hour-lim:notanumber", None, None),
        (None, None, None),
        ("", None, None),
    ],
)
def test_parse_rate_limit_is_tolerant(
    header: str | None, limit: int | None, remaining: int | None
) -> None:
    snapshot = parse_rate_limit(header)
    assert snapshot.hour_limit == limit
    assert snapshot.hour_remaining == remaining


def test_a_low_remaining_budget_starts_spacing_requests_out(
    tmp_path: Path, sleeps: list[float]
) -> None:
    header = {"x-rate-limit": f"user-hour-lim:3600;user-hour-rem:{HOUR_LOW_WATER - 1};"}
    client = build_client(
        tmp_path,
        sequence_transport([json_response({"data": []}, headers=header)]),
        sleeps=sleeps,
    )

    client.get_json("/v1/apps")  # first call observes the header
    assert client.rate_limit.is_low is True
    assert client.rate_limit.is_critical is False
    assert sleeps == [], "the call that observed the header is not itself delayed"

    client.get_json("/v1/apps")  # second call is paced
    assert sleeps == [0.25]
    assert "throttled" in client.rate_limit.describe()


def test_a_critical_budget_spaces_by_a_full_second(tmp_path: Path, sleeps: list[float]) -> None:
    header = {"x-rate-limit": f"user-hour-lim:3600;user-hour-rem:{HOUR_CRITICAL - 1};"}
    client = build_client(
        tmp_path,
        sequence_transport([json_response({"data": []}, headers=header)]),
        sleeps=sleeps,
    )
    client.get_json("/v1/apps")
    client.get_json("/v1/apps")

    assert sleeps == [1.0]
    assert "CRITICAL" in client.rate_limit.describe()


def test_the_minute_window_slides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apple's undocumented per-minute ceiling behaves like a sliding window."""
    now = [1000.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    throttle = Throttle(minute_cap=3, sleep=sleep, monotonic=lambda: now[0])
    for _ in range(3):
        assert throttle.acquire() == 0.0
    assert throttle.recent_minute_count() == 3

    # The fourth request inside the window waits for the oldest to age out.
    throttle.acquire()
    assert slept and slept[0] == pytest.approx(60.05)
    assert throttle.recent_minute_count() <= 3
    assert throttle.slept_seconds == pytest.approx(60.05)


def test_describe_when_apple_has_sent_no_header() -> None:
    assert "unknown" in RateLimitSnapshot().describe()


# --- Retry-After -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7.0),
        ("0", 0.0),
        ("-3", 0.0),
        ("Wed, 21 Oct 2026 07:28:10 GMT", 10.0),
        ("nonsense", None),
        (None, None),
    ],
)
def test_parse_retry_after_handles_both_forms(value: str | None, expected: float | None) -> None:
    import email.utils

    now = email.utils.parsedate_to_datetime("Wed, 21 Oct 2026 07:28:00 GMT").timestamp()
    assert parse_retry_after(value, now=now) == expected


def test_a_429_waits_exactly_as_long_as_apple_asked(
    tmp_path: Path, sleeps: list[float]
) -> None:
    transport = sequence_transport(
        [
            apple_error(429, code="RATE_LIMIT_EXCEEDED", headers={"retry-after": "7"}),
            json_response({"data": [resource("apps", "1")]}),
        ]
    )
    client = build_client(tmp_path, transport, sleeps=sleeps, max_retries=2)

    assert client.get_json("/v1/apps")["data"]
    assert 7.0 in sleeps, f"expected a 7s wait from Retry-After, slept {sleeps}"


def test_a_persistent_429_becomes_a_rate_limit_error_carrying_retry_after(
    tmp_path: Path, sleeps: list[float]
) -> None:
    transport = sequence_transport(
        [apple_error(429, code="RATE_LIMIT_EXCEEDED", headers={"retry-after": "30"})]
    )
    client = build_client(tmp_path, transport, sleeps=sleeps, max_retries=0)

    with pytest.raises(RateLimitError) as excinfo:
        client.get_json("/v1/apps")
    assert excinfo.value.retry_after == 30.0
    assert excinfo.value.to_dict()["retry_after_seconds"] == 30.0


def test_a_5xx_is_retried_then_reported_as_upstream(tmp_path: Path, sleeps: list[float]) -> None:
    log: list[httpx.Request] = []
    client = build_client(
        tmp_path,
        sequence_transport([apple_error(503, title="Service Unavailable")], log=log),
        sleeps=sleeps,
        max_retries=2,
    )
    with pytest.raises(UpstreamError) as excinfo:
        client.get_json("/v1/apps")

    assert len(log) == 3, "the original attempt plus two retries"
    assert excinfo.value.status == 503
    assert "system-status" in excinfo.value.remedy


def test_a_timeout_is_retried_then_explained(tmp_path: Path, sleeps: list[float]) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ReadTimeout("too slow", request=request)

    client = build_client(
        tmp_path, httpx.MockTransport(handler), sleeps=sleeps, max_retries=1
    )
    with pytest.raises(UpstreamError, match="timed out"):
        client.get_json("/v1/apps")
    assert len(attempts) == 2


# --- Error classification ----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, CredentialsError),
        (403, StorePermissionError),
        (404, NotFoundError),
        (429, RateLimitError),
        (400, ValidationError),
        (409, ValidationError),
        (422, ValidationError),
        (500, UpstreamError),
        (418, UpstreamError),
    ],
)
def test_apple_status_codes_map_onto_the_shared_taxonomy(
    status: int, expected: type[Exception]
) -> None:
    error = classify_asc_error(
        status,
        {"errors": [{"code": "SOME_CODE", "detail": "it went wrong"}]},
        context="listing apps",
    )
    assert isinstance(error, expected)
    assert error.remedy, "every error must answer 'what do I do next?'"


def test_the_403_remedy_names_the_role_that_fixes_it() -> None:
    error = classify_asc_error(403, {}, context="reading sales")
    assert "Users and Access -> Integrations" in error.remedy
    assert "Account Holder" in error.remedy


def test_the_404_remedy_explains_apple_id_versus_bundle_id() -> None:
    error = classify_asc_error(404, {}, context="reading app com.acme.todo")
    assert "numeric Apple ID" in error.remedy
    assert "filter[bundleId]" in error.remedy


def test_apple_error_details_are_flattened_into_one_actionable_line() -> None:
    error = classify_asc_error(
        409,
        {
            "errors": [
                {
                    "code": "ENTITY_ERROR.ATTRIBUTE.REQUIRED",
                    "title": "An attribute is required",
                    "detail": "You must provide a value",
                    "source": {"pointer": "/data/attributes/whatsNew"},
                }
            ]
        },
        context="updating the version",
    )
    detail = error.details["apple_detail"]
    assert "You must provide a value" in detail
    assert "/data/attributes/whatsNew" in detail
    assert error.details["apple_codes"] == ["ENTITY_ERROR.ATTRIBUTE.REQUIRED"]


def test_a_non_json_error_body_still_produces_a_usable_error(tmp_path: Path) -> None:
    transport = sequence_transport(
        [httpx.Response(400, text="<html>Gateway said no</html>")]
    )
    client = build_client(tmp_path, transport, max_retries=0)
    with pytest.raises(ValidationError) as excinfo:
        client.get_json("/v1/apps")
    assert "Gateway said no" in excinfo.value.details["apple_detail"]


def test_the_remaining_budget_is_attached_to_the_error(tmp_path: Path) -> None:
    header = {"x-rate-limit": "user-hour-lim:3600;user-hour-rem:12;"}
    transport = sequence_transport([apple_error(403, detail="nope", headers=header)])
    client = build_client(tmp_path, transport, max_retries=0)
    with pytest.raises(StorePermissionError) as excinfo:
        client.get_json("/v1/apps")
    assert excinfo.value.details["hourly_requests_remaining"] == 12


# --- Request bodies ----------------------------------------------------------


def test_resource_body_builds_json_api_documents() -> None:
    body = resource_body(
        "betaBuildLocalizations",
        attributes={"whatsNew": "Bug fixes"},
        relationships={"build": "b1", "app": ("apps", "1234567890")},
    )
    assert body["data"]["type"] == "betaBuildLocalizations"
    assert body["data"]["attributes"] == {"whatsNew": "Bug fixes"}
    assert body["data"]["relationships"]["build"]["data"] == {"type": "builds", "id": "b1"}
    assert body["data"]["relationships"]["app"]["data"] == {"type": "apps", "id": "1234567890"}


def test_post_and_patch_send_json_and_return_the_document(tmp_path: Path) -> None:
    log: list[httpx.Request] = []
    client = build_client(
        tmp_path,
        routed_transport({"/v1": json_response({"data": resource("apps", "1")})}, log=log),
    )
    assert client.post("/v1/apps", {"data": {"type": "apps"}})["data"]["id"] == "1"
    assert client.patch("/v1/apps/1", {"data": {"type": "apps"}})["data"]["id"] == "1"

    assert [r.method for r in log] == ["POST", "PATCH"]
    assert log[0].headers["content-type"] == "application/json"


def test_download_of_a_presigned_url_sends_no_bearer_token(tmp_path: Path) -> None:
    """Apple's signed CDN URLs reject an Authorization header."""
    log: list[httpx.Request] = []
    client = build_client(
        tmp_path,
        routed_transport({"/segments": httpx.Response(200, content=b"gzip-bytes")}, log=log),
    )
    assert client.download("https://cdn.apple.com/segments/abc?sig=1") == b"gzip-bytes"
    assert "authorization" not in log[0].headers


def test_a_failed_download_is_classified_too(tmp_path: Path) -> None:
    client = build_client(
        tmp_path, routed_transport({"/segments": apple_error(403, detail="expired")})
    )
    with pytest.raises(StorePermissionError):
        client.download("https://cdn.apple.com/segments/abc")


def test_client_without_credentials_defers_the_failure_to_first_use() -> None:
    """A server with only Google Play configured must still start."""
    client = AscClient(transport=httpx.MockTransport(lambda r: json_response({"data": []})))
    with pytest.raises(CredentialsError):
        client.get_json("/v1/apps")
    client.close()


def test_untyped_bodies_are_rejected_rather_than_guessed(tmp_path: Path) -> None:
    client = build_client(tmp_path, sequence_transport([httpx.Response(200, json=["a", "b"])]))
    with pytest.raises(UpstreamError, match="non-object body"):
        client.get_page("/v1/apps")
    assert client.get_json("/v1/apps") == {}


def test_context_manager_closes_the_transport(tmp_path: Path) -> None:
    with build_client(tmp_path, sequence_transport([json_response({"data": []})])) as client:
        assert client.get_json("/v1/apps") == {"data": []}
    assert client._client.is_closed
