# StorePilot

[![CI](https://github.com/sonlenef/storepilot-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/sonlenef/storepilot-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

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

Each question above is one tool call covering every app you own, because the
tools are portfolio-shaped rather than endpoint-shaped. The last one is two, on
purpose: writes preview first and wait for a human.

You need your own developer accounts — a Google Play service account, an App
Store Connect API key, or both. StorePilot reads your accounts; it does not
scrape public store listings.

> **Status: early.** The read tools have run against real accounts on both
> stores; the money and crash-threshold paths have not returned real data yet,
> and no write has ever executed against a store. Read
> [Status and honest limits](#status-and-honest-limits) before trusting a number.

---

## What is actually different

**Both stores, one tool set.** Google Play via the Android Publisher API, the
Play Developer Reporting API and the private Cloud Storage reports bucket; the
App Store via App Store Connect. 34 tools total, named consistently.

**Installs and earnings, which most tooling cannot reach.** Play installs,
ratings and earnings have no REST API. They exist only as UTF-16 CSVs in a
private Cloud Storage bucket, so most tooling skips them and "how much did this
app earn?" simply cannot be answered. StorePilot reads that bucket, resolves CSV
columns by name rather than position, and reports earnings per currency without
ever summing across currencies. (This path is implemented and fixture-tested but
has not yet read a real report — see [Status](#status-and-honest-limits).)

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

---

## Install

Requires Python 3.11+.

```bash
uvx storepilot          # no install; uv fetches it per run
```

or install it:

```bash
pipx install storepilot   # or: pip install storepilot
```

Either way you get a `storepilot` binary. Every client config below wants its
absolute path — `which storepilot` prints it.

### Claude Code

```bash
claude mcp add storepilot -- storepilot
```

### Claude Desktop / any MCP client

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "storepilot": {
      "command": "/absolute/path/to/storepilot",
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

### Cursor

Settings → MCP → Add new MCP server, or add to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "storepilot": {
      "command": "/absolute/path/to/storepilot",
      "env": { "STOREPILOT_ASC_KEY_PATH": "/path/to/AuthKey_XXXXXXXXXX.p8" }
    }
  }
}
```

### Codex CLI

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.storepilot]
command = "/absolute/path/to/storepilot"
env = { STOREPILOT_ASC_KEY_PATH = "/path/to/AuthKey_XXXXXXXXXX.p8" }
```

Configure only the store you use. Each adapter registers its tools independently,
and the cross-store tools register as soon as either store is configured. See
[`.env.example`](.env.example) for every variable the code reads.

A `.env` file also works, but it is read from the process working directory —
which is whatever directory your MCP client happened to launch the server from,
not the repo. Use it when you run `storepilot` yourself from the checkout; use
the client's `env` block otherwise.

Credentials are read from the environment and never written anywhere by
StorePilot. Keep the service account JSON and the `.p8` outside the repo, and
`chmod 600` both — they are private keys.

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
| `play_get_vitals` | Crash and ANR (Application Not Responding) rates vs Google's 1.09% / 0.47% thresholds |
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

StorePilot has been run against real accounts on both stores, read-only. That
first run found three bugs, all fixed: Apple rejects `sort` on the
appStoreVersions endpoint, the Play reports bucket is not always named
`pubsite_prod_rev_*`, and bucket access is an account-level Play Console
permission rather than the Cloud Console IAM grant the error message used to
recommend.

**Verified live** — service-account and ES256 JWT auth, rate-limit pacing,
`setup_doctor` on both stores, the Play and App Store read tools, and
`portfolio_overview` rendering six apps across both stores in one table.

**Not yet verified, and two of them carry headline claims:**

- **No vitals datapoint has ever come back.** Android Vitals suppresses metrics
  below a minimum daily user count, and the test account's apps are under it. So
  the threshold comparison — the thing `play_get_vitals` exists for — has never
  run on real numbers, and the open question stands: does the Reporting API
  return `1.09` or `0.0109`? A mismatch makes every verdict wrong by 100x.
- **No report CSV has ever been read.** Installs, ratings and earnings all come
  from the GCS bucket, and the parser has only ever seen fixtures. Google changed
  the earnings report's Fee Description column in [July 2026][play-reports], so
  the real headers are the risk.

[play-reports]: https://support.google.com/googleplay/android-developer/answer/6135870
- **No write has ever executed.** Every guard, preview and rollout policy is
  proven against a fake client and 503 tests, never against a real store.
- Apple sales reports return 403 for an App Manager key; reading them needs a
  key with finance access.

The full list is in [docs/ROADMAP.md](docs/ROADMAP.md). If you run StorePilot
against a real account — especially one with an app large enough to report
vitals — those items are the most valuable bug reports you can file.

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
  refusing around 300–350. The client paces against both, holding itself to 240
  requests a minute and spacing calls out as the hourly budget runs down.
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
- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, ground rules, and what actually helps
- [CHANGELOG.md](CHANGELOG.md) — what shipped, and what the live runs fixed

## Contributing

The most useful contribution is running StorePilot against a real store account
and reporting what breaks — particularly an account with an app large enough that
Android Vitals reports data, which the test account was not. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## MCP Registry

Published as `io.github.sonlenef/storepilot-mcp`.

mcp-name: io.github.sonlenef/storepilot-mcp

## License

MIT
