# StorePilot Roadmap

**Current state: 34 tools across both stores, feature-complete for the initial
scope.** The read tools have run against real accounts on both stores. The money
paths, the vitals thresholds and every write are still unproven against a live
store — everything marked DONE below was built and unit-tested against fakes and
fixtures. The [live-verification backlog](#live-credential-verification-backlog)
is the real remaining work.

## Phase 1 — Google Play read-only — DONE (read tools run live; money paths unproven)

- [x] Auth: service account loading + Android Publisher / Reporting API clients
- [x] `play_list_apps`
- [x] `play_get_vitals` — thresholds applied (crash 1.09%, ANR 0.47%), user-weighted
- [x] `play_get_anomalies` — Google's own anomaly detections
- [x] `play_get_stats` — GCS bucket CSV (installs, ratings); resolves columns by name
- [x] `play_get_earnings` — GCS bucket CSV, grouped per currency
- [x] `play_list_reviews`
- [x] `play_portfolio_health` — the flagship portfolio-wide scan
- [x] `setup_doctor`: real per-step checks with a remedy for each failure

## Phase 2 — App Store Connect — DONE (read tools run live; sales unproven)

- [x] Auth: ES256 JWT, 20-minute ceiling, re-minted at 15
- [x] Rate limiting paced from the `x-rate-limit` header plus the undocumented
      per-minute ceiling
- [x] `asc_list_apps`, `asc_list_builds` (TestFlight), `asc_list_versions`
- [x] `asc_get_sales` — sales/finance reports (gzip CSV), cached hard
- [x] `asc_get_analytics` — the four-level asynchronous chain, resumable
- [x] `asc_list_reviews`
- [x] `asc_upload_build` — returns Transporter instructions; no REST endpoint
      accepts a binary
- [x] `setup_doctor` extended with the ASC steps

## Phase 3 — Cross-store — DONE (`portfolio_overview` run live; the rest unproven)

- [x] Pairing registry at `~/.storepilot/apps.toml` + `list_app_pairs`,
      `suggest_app_pairs`, `pair_apps`
- [x] `portfolio_overview` — every app, both stores, one table, per-cell reason
      codes
- [x] `compare_reviews` — both stores' reviews side by side
- [x] `parity_check` — version & metadata drift between stores
- [x] `metadata_pull` / `metadata_push` — fastlane-layout mirror, digest-compared
- [ ] README demo recordings of `portfolio_overview` and `release_both` — the
      second is blocked until a write has run against a real store
- [ ] Launch posts (r/FlutterDev, r/androiddev, r/iOSProgramming, MCP registries)

## Phase 4 — Guarded writes — DONE (no write has ever executed against a store)

- [x] Guard framework: HMAC-keyed, single-use, content-bound confirmation tokens;
      production forced to a staged rollout; audit log at `~/.storepilot/audit.log`
- [x] `PlayEdit` edits lifecycle with a dry-run mode that makes previews truthful
- [x] `play_upload_bundle`, `play_create_release` (internal track default)
- [x] `play_promote_release`, `play_expand_rollout`
- [x] `play_halt_rollout` (the fire-escape button — deliberately unguarded)
- [x] `play_reply_review`, `play_update_listing`
- [x] `asc_reply_review`, `asc_update_metadata`, `asc_submit_for_review`
      (with a blocking local precheck)
- [x] `release_both` — one call, one token, both stores; partial failure reported
      exactly, nothing rolled back

## Live-credential verification backlog

**Verified live on 2026-08-17**, read-only, against real accounts on both stores
— an App Store Connect account (4 apps, App Manager key) and a Google Play
account:

- Auth on both sides: service-account credentials, ES256 JWT minting,
  `x-rate-limit` header parsing and rate-limit pacing.
- `setup_doctor` end to end on both stores.
- The App Store read tools: `asc_list_apps`, `asc_list_builds`,
  `asc_list_reviews`, `asc_list_versions`.
- The Play read tools that do not depend on the reports bucket or on vitals data
  being published.
- `portfolio_overview` rendering six apps across both stores in one table,
  including its degradation path for cells it could not fill.

Three bugs surfaced and were fixed:

1. Apple rejects `sort` on the appStoreVersions relationship endpoint, which made
   `asc_list_versions` fail outright.
2. The reports bucket is not always named `pubsite_prod_rev_*`; a live account
   returned `pubsite_prod_<accountId>`.
3. Bucket access is an account-level Play Console permission, not the Cloud
   Console IAM grant the 403 remedy used to recommend.

**Everything below is still unverified.** In rough risk order — the first two
would invalidate a headline claim if they came back wrong:

- **No vitals datapoint has ever come back.** Android Vitals suppresses metrics
  below a minimum daily user count and the test account's apps are under it, so
  the threshold comparison `play_get_vitals` exists for has never run on real
  numbers. The open question underneath: do the Reporting API's rate metrics come
  back as percentages (`1.09`) or fractions (`0.0109`)? A mismatch makes every
  threshold verdict wrong by 100x. Needs an account with a large enough app.
- Does deleting a dry-run edit truly discard the uploaded bundle without
  consuming the version code? The whole truthful-preview strategy rests on it.
- Does `edits.validate` reject everything `commit` would?
- Real GCS object names under the earnings prefix, and real CSV column headers.
- Resumable upload of a multi-hundred-MB AAB, including mid-upload 5xx handling.
- Does the Reporting API's `apps.search` really omit apps with no published
  release, as assumed by `setup_doctor`'s zero-apps remedy?
- Apple `salesReports`: real column names, and whether the units × per-unit
  proceeds calculation matches the figure App Store Connect displays. A first
  live attempt returned `FORBIDDEN_ERROR`: an **App Manager** key cannot read
  sales reports, so this needs a key with finance access to verify at all.
- Apple analytics: the real latency from `create=True` to the first ONGOING
  report instance (assumed 24–48h), and segment download shape.
- Play locale codes actually returned for listings, against the mapping table.
- Whether `changes_not_sent_for_review` behaves as documented when a release is
  already in review.

If you run StorePilot against a real account, these are the most valuable bug
reports you can file.

## Known hard limits (API-level, cannot be fixed by us)

- Creating a brand-new app record requires the console UI on both stores
  (first upload manual on Play).
- Declaration forms (Data safety, privacy labels, content rating) are console-only.
- Play reviews API: production track only, comment-bearing reviews only, roughly
  the last 7 days only.
- No REST endpoint uploads an iOS binary; Transporter/altool only.
- Apple publishes no aggregate rating through its API.
- Play report data lags 3–7 days; monthly earnings land around the 5th of the
  following month.
- Quotas: Play Reporting 10 QPS; App Store Connect ~3,600 req/hour plus an
  undocumented ~300/min ceiling, with `salesReports` far scarcer than the rest.

## Not planned

Out of scope, so nobody waits for them. Other MCP servers cover several of these
— see [COMPARISON.md](COMPARISON.md):

- In-app purchases, subscriptions and pricing on either store
- Screenshot generation or upload
- Code signing, certificates, provisioning profiles, device registration
- TestFlight tester and beta-group management
- CI-driven release automation — that is fastlane's job, and the metadata layout
  is deliberately shared so both can run against one checkout
