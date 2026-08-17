# StorePilot

One MCP server for a whole app portfolio, across Google Play and the App Store.

Ask your assistant:

> "Which of my apps has a crash rate above Google's threshold?"
>
> "How much did the portfolio earn last month, per app?"
>
> "Show me every app on both stores: version, rating, installs, revenue, crashes."
>
> "The German description drifted between the two stores — what's different?"
>
> "Ship 3.2.1 to the Play internal track and TestFlight."

It answers those in one call each, for every app you own, because the tools are
portfolio-shaped rather than endpoint-shaped.

> **Status: not yet verified against a live store account.** Every tool is
> implemented and unit-tested, and nothing has run against real credentials.
> Treat the first run as the verification. See
> [Status and honest limits](#status-and-honest-limits).

---

## What is actually different

**Both stores, one tool set.** Google Play via the Android Publisher API, the
Play Developer Reporting API and the private Cloud Storage reports bucket; the
App Store via App Store Connect. 34 tools total, named consistently.

**Money and installs at all.** Play installs, ratings and earnings have no REST
API. They exist only as UTF-16 CSVs in a private GCS bucket, so most tooling
skips them and "how much did this app earn?" simply cannot be answered. StorePilot
reads that bucket, resolves CSV columns by name, and reports earnings per
currency without ever summing across currencies.

**Portfolio-first.** `portfolio_overview` renders every app on both stores in
one table. `play_portfolio_health` scans an entire Play account. Neither takes a
package name. Built for indie devs, app factories and agencies.

**Cross-store tools.** `parity_check` finds listing and version drift between
the two stores. `compare_reviews` puts both stores' reviews side by side.
`release_both` ships one version to both. `metadata_pull` / `metadata_push` use
fastlane's own directory layout, so adopting StorePilot never means abandoning
fastlane.

**Writes are gated.** Every write previews first and returns a confirmation
token that is HMAC-keyed to the exact operation, single-use, and expires in 10
minutes. Production releases are forced into a staged rollout capped at 20%.

**`setup_doctor`.** Credential setup here is a multi-step Google Cloud + Play
Console + App Store Connect maze with several silent failure modes. One tool
checks every step and prints the exact fix.

<!-- TODO(demo): GIF of portfolio_overview rendering a real multi-app portfolio,
     recorded once live credentials are available. -->

<!-- TODO(demo): GIF of release_both — preview block, human approval, both
     stores landing. Recorded from the same session. -->

---

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/<you>/storepilot-mcp
cd storepilot-mcp
python -m venv .venv && .venv/bin/pip install -e .
```

Not yet on PyPI, so `pip install storepilot` does not work yet.

### Claude Code

```bash
claude mcp add storepilot -- /absolute/path/to/storepilot-mcp/.venv/bin/storepilot
```

### Claude Desktop / any MCP client

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "storepilot": {
      "command": "/absolute/path/to/storepilot-mcp/.venv/bin/storepilot",
      "env": {
        "STOREPILOT_GOOGLE_CREDENTIALS": "/path/to/service-account.json",
        "STOREPILOT_GOOGLE_REPORTS_BUCKET": "pubsite_prod_rev_0123456789",
        "STOREPILOT_ASC_KEY_PATH": "/path/to/AuthKey_XXXXXXXXXX.p8",
        "STOREPILOT_ASC_KEY_ID": "XXXXXXXXXX",
        "STOREPILOT_ASC_ISSUER_ID": "00000000-0000-0000-0000-000000000000",
        "STOREPILOT_ASC_VENDOR_NUMBER": "80000000"
      }
    }
  }
}
```

Configure only the store you use. Each adapter registers its tools independently,
and the cross-store tools register as soon as either store is configured.
`.env` in the project directory works too — see [`.env.example`](.env.example)
for every variable the code reads.

---

## Start with setup_doctor

Do not start by calling a data tool and guessing why it returned nothing. Ask
your assistant to run `setup_doctor` first. It checks each credential step
independently, so one failure never hides the rest, and every failure carries
its own fix.

With nothing configured yet:

```
StorePilot setup check
============================================================

-- Google Play ----------------------------------------------
[skip] Google Play: not configured
      Fix: Set STOREPILOT_GOOGLE_CREDENTIALS to the path of a service account JSON key. Full
      setup: create a Google Cloud project, enable the Android Publisher and Play Developer
      Reporting APIs, create a service account, download its JSON key, then invite the service
      account email in Play Console -> Users and permissions.
      Docs: https://developers.google.com/android-publisher/getting_started

-- App Store Connect ----------------------------------------
[fail] ASC credentials: App Store Connect is not configured: STOREPILOT_ASC_KEY_PATH,
      STOREPILOT_ASC_KEY_ID, STOREPILOT_ASC_ISSUER_ID unset.
      Fix: In App Store Connect go to Users and Access -> Integrations -> App Store Connect
      API, create a team key with the Admin or App Manager role, and download the .p8 file
      (Apple lets you download it exactly once). Then set STOREPILOT_ASC_KEY_PATH to that
      file, STOREPILOT_ASC_KEY_ID to the Key ID column, and STOREPILOT_ASC_ISSUER_ID to the
      Issuer ID shown above the key table. Sales reports additionally need
      STOREPILOT_ASC_VENDOR_NUMBER from Payments and Financial Reports.
      Docs: https://developer.apple.com/documentation/appstoreconnectapi/creating-api-keys-for-app-store-connect-api
[skip] ASC token: cannot test until the credentials load
      Fix: Fix the step above, then re-run setup_doctor.
[skip] ASC API reachable: cannot test until the credentials load
      Fix: Fix the step above, then re-run setup_doctor.
[skip] ASC sales access: cannot test until the credentials load
      Fix: Fix the step above, then re-run setup_doctor.

============================================================
Summary: 1 fail, 4 skip
Resolve the [fail] items above, then run setup_doctor again.
```

It also catches the failure that looks like success: without the **Reply to
reviews** permission, Play's reviews API returns HTTP 200 and an empty list
rather than an error, so "this app has no reviews" and "you lack a permission"
are the same response. `setup_doctor` reports that ambiguity as its own
diagnosis instead of guessing.

Full walkthrough for both stores: **[docs/SETUP.md](docs/SETUP.md)**.

---

## Tools

34 tools. Full arguments, return shapes and per-tool limitations in
**[docs/TOOLS.md](docs/TOOLS.md)**.

### Google Play — read

| Tool | What it answers |
|---|---|
| `play_list_apps` | Which packages can this install reach? |
| `play_get_vitals` | Crash and ANR rates vs Google's 1.09% / 0.47% thresholds |
| `play_get_anomalies` | What did Google's own anomaly detection flag? |
| `play_get_stats` | Installs and ratings for a month (GCS bucket) |
| `play_get_earnings` | Earnings for a month, per currency (GCS bucket) |
| `play_list_reviews` | Recent production reviews, filterable by star rating |
| `play_portfolio_health` | Vitals + rating + installs for every app, one table |

### Google Play — write (all gated)

| Tool | What it does |
|---|---|
| `play_upload_bundle` | Upload an .aab and release it on a track |
| `play_create_release` | Release already-uploaded version codes |
| `play_promote_release` | Move a build between tracks (beta → production) |
| `play_expand_rollout` | Widen a staged rollout — the only path to 100% |
| `play_halt_rollout` | Stop a rollout **immediately**, no confirmation step |
| `play_reply_review` | Publish a public developer reply |
| `play_update_listing` | Overwrite listing copy for one locale |

### App Store Connect

| Tool | What it does |
|---|---|
| `asc_list_apps` | Apps visible to the key, with Apple IDs |
| `asc_list_builds` | TestFlight builds and their three review states |
| `asc_list_reviews` | Customer reviews, filterable, full history |
| `asc_list_versions` | Versions, review state, phased-release day |
| `asc_get_sales` | Sales and proceeds, cached hard |
| `asc_get_analytics` | App Analytics via Apple's async reports chain |
| `asc_upload_build` | Returns Transporter instructions — no API can upload |
| `asc_reply_review` | Public developer response (gated) |
| `asc_update_metadata` | Listing copy on the editable version (gated) |
| `asc_submit_for_review` | Submit to App Review after a precheck (gated) |

### Cross-store

| Tool | What it does |
|---|---|
| `portfolio_overview` | Every app on both stores in one table |
| `compare_reviews` | Both stores' reviews for one app, side by side |
| `parity_check` | Version and listing drift between the stores |
| `release_both` | One version to Play and TestFlight, one token (gated) |
| `metadata_pull` | Store copy → fastlane-layout local tree |
| `metadata_push` | Local tree → both stores (gated) |
| `list_app_pairs` | Show the pairing registry |
| `suggest_app_pairs` | Propose pairings from bundle-id and name evidence |
| `pair_apps` | Write one pairing into the registry |

Plus `setup_doctor`.

Nothing in either API says a Play package and an Apple ID are the same product,
so the pairing lives in `~/.storepilot/apps.toml`. Run `suggest_app_pairs`, then
`pair_apps` for the proposals you agree with. A proposal is inert until written —
a wrong pair would attribute one app's revenue, reviews and crash rate to
another, and nothing downstream would look wrong.

---

## The safety model

Every write is two calls. The first returns a preview and a token; the second
carries that token back.

The preview is the actual safety mechanism: it renders into the chat, where a
human can read "that is the wrong app" or "that is the production track". Play
previews are not estimates — the operation runs for real inside a throwaway Play
edit that is validated and then deleted, so the preview reports the version code
Google actually assigned and any error Google would actually raise.

The token exists only to prevent drift between the preview and the confirmation.
It is an HMAC over a canonical fingerprint of the operation, keyed with a
per-install secret kept out of tool output, single-use, and valid for 10 minutes.
A plain content hash would be computable by the model itself, letting it
self-confirm without ever rendering the preview a human needs to see. Change any
argument and the token stops working.

Three more rules:

- **Production is forced into a staged rollout**, capped at 20% on the first
  step (0.1 if you omit a fraction). `play_expand_rollout` is the only path to
  100%, separated so it can never happen as a side effect.
- **`play_halt_rollout` is deliberately ungated.** Stopping a bad release is
  always the safe direction, and making someone do a second round trip during an
  incident is a design failure.
- **Everything is audited** — previewed, confirmed, rejected, executed, failed —
  to an append-only log at `~/.storepilot/audit.log`, with credentials and long
  values redacted.

`release_both` issues **one** token for both stores, because approving half a
two-store release is not approving the release. On partial failure it says
exactly what landed where and rolls nothing back: pulling a working build from
users because the other store returned an API error is worse than the drift.

---

## Status and honest limits

**Nothing here has run against a live store account.** The code is complete and
unit-tested; the assumptions it makes about real API responses have not been
checked against real API responses. The highest-risk unknowns:

- Do the Reporting API's rate metrics come back as percentages (`1.09`) or
  fractions (`0.0109`)? A mismatch makes every threshold verdict wrong by 100x.
- Does deleting a dry-run edit truly discard the uploaded bundle without
  consuming the version code? The truthful-preview strategy rests on it.
- Real GCS object names under the earnings prefix, and real CSV column headers.
- Resumable upload of a multi-hundred-MB AAB, including mid-upload 5xx handling.

The full list is in [docs/ROADMAP.md](docs/ROADMAP.md). If you run StorePilot
against a real account, those items are the most valuable bug reports you can
file.

### Hard limits, imposed by the stores

These cannot be fixed here, and StorePilot says so at the point of use rather
than returning something that looks like an answer:

- **Creating a new app record is Console-only on both stores.** On Play the
  first upload must also be manual.
- **Declaration forms are Console-only** — Data safety, privacy nutrition
  labels, content rating.
- **No REST endpoint uploads an iOS binary.** `asc_upload_build` returns
  Transporter/altool instructions instead of pretending.
- **Play's reviews API** returns production-track reviews only, comment-bearing
  reviews only, roughly the last 7 days only.
- **Apple publishes no aggregate rating** through its API at all.
- **Play report data lags 3–7 days**, and a month's earnings land around the 5th
  of the following month. Before then the report does not exist, and StorePilot
  reports "not published" rather than 0.

### Quotas

- Play Developer Reporting: 10 queries/second. StorePilot paces itself at 8 to
  leave headroom, so a large portfolio scan takes a few seconds.
- App Store Connect: ~3,600 requests/hour per key, reported in the
  `x-rate-limit` header, plus an undocumented per-minute ceiling that starts
  refusing around 300–350. The client paces against both.
- Apple's `salesReports` is far scarcer than the rest of the API, so results are
  cached — past periods forever, since Apple never rewrites them.

---

## How it compares

StorePilot is not the only MCP server touching these APIs, and it is not a
fastlane replacement. [docs/COMPARISON.md](docs/COMPARISON.md) names the
alternatives, says what each does better, and is specific about the narrow band
where StorePilot is the only option.

## Documentation

- [docs/SETUP.md](docs/SETUP.md) — the credential maze, end to end
- [docs/TOOLS.md](docs/TOOLS.md) — every tool, argument and limitation
- [docs/COMPARISON.md](docs/COMPARISON.md) — alternatives, and fastlane
- [docs/ROADMAP.md](docs/ROADMAP.md) — state and the live-verification backlog
- [docs/SECURITY.md](docs/SECURITY.md) — credential handling and the threat model

## License

MIT
