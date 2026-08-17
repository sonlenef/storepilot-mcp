"""A fake ``googleapiclient`` discovery client.

The real client is synchronous and duck-typed — ``client.edits().insert(...)``
returns a request object whose ``.execute()`` performs the call — so a fake needs
no library at all, only the same shape. Every call appends to a shared log, which
is what makes call *ordering* assertable: the whole safety property of
``PlayEdit`` is "validate before commit, delete on failure, never both".
"""

from __future__ import annotations

from typing import Any


class FakeRequest:
    """One pending API call. Records itself when executed."""

    def __init__(self, log: list[str], name: str, kwargs: dict[str, Any], result: Any) -> None:
        self.log = log
        self.name = name
        self.kwargs = kwargs
        self.result = result

    def execute(self) -> Any:
        self.log.append(self.name)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def next_chunk(self) -> tuple[None, Any]:
        """Resumable-upload protocol: one chunk, done."""
        self.log.append(self.name)
        if isinstance(self.result, Exception):
            raise self.result
        return None, self.result


class FakeEdits:
    """``client.edits()`` — the transaction surface."""

    def __init__(
        self,
        log: list[str],
        track_body: dict[str, Any] | None = None,
        *,
        fail_on: str | None = None,
        error: Exception | None = None,
        listing: dict[str, Any] | None = None,
        edit_id: str = "EDIT-1",
    ) -> None:
        self.log = log
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.track_body = track_body or {"track": "production", "releases": []}
        self.fail_on = fail_on
        self.error = error or RuntimeError(f"boom in {fail_on}")
        self.listing = listing or {
            "language": "en-US",
            "title": "Acme Todo",
            "shortDescription": "Old short description",
            "fullDescription": "Old full description",
        }
        self.edit_id = edit_id

    def _req(self, name: str, result: Any = None, **kwargs: Any) -> FakeRequest:
        self.calls.append((name, kwargs))
        if self.fail_on == name:
            return FakeRequest(self.log, name, kwargs, self.error)
        return FakeRequest(self.log, name, kwargs, result)

    # -- lifecycle --

    def insert(self, **kwargs: Any) -> FakeRequest:
        return self._req(
            "insert", {"id": self.edit_id, "expiryTimeSeconds": "9999999999"}, **kwargs
        )

    def validate(self, **kwargs: Any) -> FakeRequest:
        return self._req("validate", {"id": self.edit_id}, **kwargs)

    def commit(self, **kwargs: Any) -> FakeRequest:
        return self._req("commit", {"id": self.edit_id}, **kwargs)

    def delete(self, **kwargs: Any) -> FakeRequest:
        return self._req("delete", {}, **kwargs)

    # -- sub-resources --

    def tracks(self) -> Any:
        outer = self

        class Tracks:
            def get(self, **kwargs: Any) -> FakeRequest:
                return outer._req("tracks.get", outer.track_body, **kwargs)

            def update(self, **kwargs: Any) -> FakeRequest:
                return outer._req("tracks.update", kwargs.get("body"), **kwargs)

        return Tracks()

    def bundles(self) -> Any:
        outer = self

        class Bundles:
            def upload(self, **kwargs: Any) -> FakeRequest:
                return outer._req(
                    "bundles.upload",
                    {"versionCode": 4501, "sha256": "deadbeef", "sha1": "cafe"},
                    **kwargs,
                )

        return Bundles()

    def apks(self) -> Any:
        outer = self

        class Apks:
            def upload(self, **kwargs: Any) -> FakeRequest:
                return outer._req("apks.upload", {"versionCode": 4501}, **kwargs)

        return Apks()

    def listings(self) -> Any:
        outer = self

        class Listings:
            def get(self, **kwargs: Any) -> FakeRequest:
                return outer._req("listings.get", outer.listing, **kwargs)

            def patch(self, **kwargs: Any) -> FakeRequest:
                return outer._req("listings.patch", kwargs.get("body"), **kwargs)

            def update(self, **kwargs: Any) -> FakeRequest:
                return outer._req("listings.update", kwargs.get("body"), **kwargs)

            def list(self, **kwargs: Any) -> FakeRequest:
                return outer._req("listings.list", {"listings": [outer.listing]}, **kwargs)

        return Listings()


class FakeClient:
    """``build('androidpublisher', ...)`` — only the pieces StorePilot uses."""

    def __init__(
        self,
        log: list[str] | None = None,
        track_body: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.log: list[str] = log if log is not None else []
        self._edits = FakeEdits(self.log, track_body, **kwargs)
        self.reviews_payload: dict[str, Any] = {"reviews": []}

    def edits(self) -> FakeEdits:
        return self._edits

    def applications(self) -> Any:
        outer = self

        class Applications:
            def tracks(self) -> Any:
                class Tracks:
                    def releases(self) -> Any:
                        class Releases:
                            def list(self, **kwargs: Any) -> FakeRequest:
                                return outer._edits._req(
                                    "applications.tracks.releases.list",
                                    {"releases": outer._edits.track_body.get("releases", [])},
                                    **kwargs,
                                )

                        return Releases()

                return Tracks()

        return Applications()

    def reviews(self) -> Any:
        outer = self

        class Reviews:
            def list(self, **kwargs: Any) -> FakeRequest:
                return outer._edits._req("reviews.list", outer.reviews_payload, **kwargs)

            def reply(self, **kwargs: Any) -> FakeRequest:
                return outer._edits._req(
                    "reviews.reply", {"result": {"replyText": "ok"}}, **kwargs
                )

        return Reviews()


class FakeReportingClient:
    """``playdeveloperreporting`` — apps.search, vitals metric sets, anomalies."""

    def __init__(
        self,
        *,
        apps: list[dict[str, Any]] | None = None,
        rows: dict[str, list[dict[str, Any]]] | None = None,
        freshness: dict[str, Any] | None = None,
        errors: dict[str, Exception] | None = None,
        anomalies: list[dict[str, Any]] | None = None,
    ) -> None:
        self.apps_payload = {"apps": apps or []}
        self.rows = rows or {}
        self.freshness_payload = freshness or {}
        self.errors = errors or {}
        self.anomalies_payload = {"anomalies": anomalies or []}
        self.log: list[str] = []
        #: (call name, kwargs) for every request built, so a test can assert on
        #: the request body — which is where this API is easiest to get wrong.
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def apps(self) -> Any:
        outer = self

        class Apps:
            def search(self, **kwargs: Any) -> FakeRequest:
                outer.calls.append(("apps.search", kwargs))
                return FakeRequest(outer.log, "apps.search", kwargs, outer.apps_payload)

        return Apps()

    def anomalies(self) -> Any:
        outer = self

        class Anomalies:
            def list(self, **kwargs: Any) -> FakeRequest:
                outer.calls.append(("anomalies.list", kwargs))
                return FakeRequest(outer.log, "anomalies.list", kwargs, outer.anomalies_payload)

        return Anomalies()

    def vitals(self) -> Any:
        outer = self

        class Vitals:
            def __getattr__(self, accessor: str) -> Any:
                def build() -> Any:
                    class MetricSet:
                        def get(self, **kwargs: Any) -> FakeRequest:
                            outer.calls.append((f"{accessor}.get", kwargs))
                            return FakeRequest(
                                outer.log,
                                f"{accessor}.get",
                                kwargs,
                                outer.errors.get(f"{accessor}.get")
                                or outer.freshness_payload.get(accessor, {}),
                            )

                        def query(self, **kwargs: Any) -> FakeRequest:
                            outer.calls.append((f"{accessor}.query", kwargs))
                            return FakeRequest(
                                outer.log,
                                f"{accessor}.query",
                                kwargs,
                                outer.errors.get(f"{accessor}.query")
                                or {"rows": outer.rows.get(accessor, [])},
                            )

                    return MetricSet()

                return build

        return Vitals()


def freshness_payload(latest_end: tuple[int, int, int]) -> dict[str, Any]:
    """The ``freshnessInfo`` block a metric set's ``get`` returns."""
    year, month, day = latest_end
    return {
        "freshnessInfo": {
            "freshnesses": [
                {
                    "aggregationPeriod": "DAILY",
                    "latestEndTime": {"year": year, "month": month, "day": day},
                }
            ]
        }
    }


def timeline_row(
    day: tuple[int, int, int], metrics: dict[str, float | None]
) -> dict[str, Any]:
    """One row of a vitals timeline, in the API's ``google.type.Decimal`` shape."""
    year, month, dom = day
    return {
        "startTime": {"year": year, "month": month, "day": dom},
        "metrics": [
            {"metric": name, "decimalValue": {"value": str(value)}}
            for name, value in metrics.items()
            if value is not None
        ],
    }
