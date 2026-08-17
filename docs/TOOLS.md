# Tool reference

34 tools. Everything below is derived from the tool docstrings in the source,
which are the LLM-facing spec.

- **Google Play** tools register when `STOREPILOT_GOOGLE_CREDENTIALS` is set.
- **App Store Connect** tools register when `STOREPILOT_ASC_KEY_PATH`,
  `STOREPILOT_ASC_KEY_ID` and `STOREPILOT_ASC_ISSUER_ID` are all set.
- **Cross-store** tools register when *either* store is configured — they are
  designed to still answer with one store missing.

Tools marked **gated** are two-step: call once to get a preview and a
confirmation token, then call again with identical arguments plus `confirm=true`
and that token. See [the safety model](#the-safety-model) at the bottom.

**How much of this is proven:** the read tools have run against real accounts on
both stores. No write tool has ever executed against a store, no vitals datapoint
and no Play report CSV has ever come back, and Apple's sales reports returned 403
for the key they were tried with. Return shapes for those paths are what the code
produces against fixtures, not what a store has been observed to return. The full
list is in [ROADMAP.md](ROADMAP.md).

---

## Contents

- [Diagnostics](#diagnostics)
- [Google Play — read](#google-play--read)
- [Google Play — write](#google-play--write)
- [App Store Connect](#app-store-connect)
- [Cross-store](#cross-store)
- [The safety model](#the-safety-model)

---

## Diagnostics

### `setup_doctor()`

No arguments.

Runs every credential step for both stores independently and reports all of
them, so a single failure never hides the rest. Each check returns `[ok]`,
`[warn]`, `[fail]` or `[skip]`, and every non-ok result carries the exact fix.

Google Play checks: the key file parses (and prints the email you must grant
access to), Android Publisher is reachable, Play Developer Reporting is
reachable and lists your apps, the "Reply to reviews" permission is present, and
the Cloud Storage reports bucket is configured and readable.

App Store checks: credentials load, an ES256 token with valid claims can be
minted, `GET /v1/apps` succeeds, the vendor number is configured, and the write
audit log is writable.

**Run this first whenever any tool returns empty or unexpected data.** Empty
results are ambiguous on both stores and this tool exists to disambiguate them.
It deliberately does not probe Apple's `salesReports` endpoint — that quota is
too scarce to spend on a diagnostic.

Returns a plain-text report. Full walkthrough: [SETUP.md](SETUP.md).

---

## Google Play — read

### `play_list_apps()`

No arguments. Start here — every other Play tool takes a `package_name`.

Returns a table of app name and package name.

**Limitation:** source is Play Developer Reporting `apps.search`, which lists
only apps that the service account has been granted access to **and** that have
at least one published release. A draft app never appears. That is a Google
limitation, not a permission problem.

### `play_get_vitals(package_name, days=28)`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `package_name` | str | — | e.g. `com.example.app`, from `play_list_apps` |
| `days` | int | `28` | Trailing window |

Returns the user-perceived, user-weighted crash and ANR rates — the exact
figures Play Console judges an app on — each with an explicit verdict against
Google's bad-behaviour thresholds (**crash 1.09%**, **ANR 0.47%**), plus the
metric name, distinct user count and last covered day.

**Notes and limitations:**

- `days=28` and `days=7` map onto Google's own rolling user-weighted averages and
  are the most trustworthy. Other values are averaged locally, weighted by daily
  user counts — a plain mean over daily rates lets one low-traffic day invent a
  threshold breach the Console never shows.
- Data trails real time by roughly 2–3 days.
- Google suppresses vitals entirely for apps below a minimum daily user count.
  For a low-traffic app, "no data" does not mean "no crashes".

### `play_get_anomalies(package_name)`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `package_name` | str | — | From `play_list_apps` |

Returns the deviations **Google's own detection** flagged — a crash spike on a
specific Android version or device model, for example — with the observed value,
the expected range, and the dimensions it was concentrated in.

Higher signal than a threshold check alone: it catches a regression that is bad
relative to the app's own baseline while still under the absolute threshold. An
empty result is genuinely good news.

### `play_get_stats(package_name, month="")`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `package_name` | str | — | From `play_list_apps` |
| `month` | str | last complete month | `"YYYY-MM"`, e.g. `"2026-07"` |

Returns installs and ratings for one calendar month.

**Limitations:**

- **Requires `STOREPILOT_GOOGLE_REPORTS_BUCKET`** plus the account-level Play
  Console permission "View app information and download bulk reports". No Play
  REST API serves this data — it exists only as CSVs in the reports bucket, and
  per-app permission grants do not reach it. See [SETUP.md](SETUP.md#step-3--the-reports-bucket-installs-ratings-earnings).
- Play stats land 3–7 days late, so the current month is always partial and is
  labelled as such.
- Installs and ratings are fetched independently: if one report is missing the
  other is still reported, with a note explaining the gap.

### `play_get_earnings(month="", package_name="")`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `month` | str | last complete month | `"YYYY-MM"` |
| `package_name` | str | `""` (whole account) | Narrow to one app |

Returns totals per currency, and — for an account-wide call — a per-product
breakdown, plus the transaction line count and source object.

**Limitations:**

- Same bucket requirement as `play_get_stats`, plus the account-level "View
  financial data, orders, and cancellation survey responses" permission.
- Google publishes a month's earnings around the **5th of the following month**.
  Before then the report does not exist, and this tool says "not published"
  rather than reporting `0`. Those are different facts.
- Amounts are **merchant currency** (the post-Google-fee payout basis), not list
  price.
- Totals across different currencies are **never** added together. There is no
  exchange rate here, and inventing one produces a number that looks right and
  is not.

### `play_list_reviews(package_name, min_rating=1, max_rating=5, limit=50)`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `package_name` | str | — | From `play_list_apps` |
| `min_rating` | int | `1` | Clamped to 1–5 |
| `max_rating` | int | `5` | Clamped to 1–5; set both to 1 to triage the angriest users |
| `limit` | int | `50` | Clamped to 1–100 |

Returns each review with stars, date, author, app version, device, review text,
and whether a developer already replied.

**Limitations, all imposed by Google:**

- **Production track only.** Testing-track feedback is invisible to this API.
- **Comment-bearing reviews only.** A bare star rating with no text never
  appears, so this is not a way to count ratings — use `play_get_stats` for the
  rating average.
- **Roughly the last 7 days only.**
- Without the **Reply to reviews** permission the API returns HTTP 200 and an
  empty list rather than an error. An empty result is therefore ambiguous, and
  this tool says so instead of claiming the app has no reviews.

### `play_portfolio_health(month="", days=28)`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `month` | str | last complete month | Month for the installs/rating columns |
| `days` | int | `28` | Vitals window; 28 matches Play Console |

One call, every app in the Play account. Gathers vitals against Google's
thresholds, the latest average rating and the month's installs, then flags the
apps needing attention. Takes no package name.

**Notes:**

- Each app is fetched independently. A failure on one app becomes an error note
  in its row rather than failing the report — a missing permission on one app
  must not hide the other twenty.
- Requests are throttled under the Reporting API's 10 QPS quota, so a large
  portfolio takes a few seconds.
- Rating and install columns need the reports bucket. Without it the vitals
  columns still work and the rest reads `n/a`.

For both stores in one table, use [`portfolio_overview`](#portfolio_overviewmonth-days28).

---

## Google Play — write

All gated except `play_halt_rollout`.

### `play_upload_bundle(...)` — gated

| Argument | Type | Default |
|---|---|---|
| `package_name` | str | — |
| `aab_path` | str | — |
| `track` | str | `"internal"` |
| `release_notes` | str \| None | `None` |
| `release_notes_locale` | str | `"en-US"` |
| `confirm` | bool | `False` |
| `confirmation_token` | str \| None | `None` |

Uploads an `.aab` and releases it on a track.

On the preview leg nothing is published: the bundle is pushed into a **throwaway
Play edit that is validated and then discarded**, so the preview reports the
version code Google actually assigned and any error Google would actually raise
— not an estimate.

`track` defaults to `internal`. `track="production"` forces a staged rollout of
at most 20% on the first step.

### `play_create_release(...)` — gated

| Argument | Type | Default |
|---|---|---|
| `package_name` | str | — |
| `track` | str | — |
| `version_codes` | list[str] | — |
| `release_notes` | str \| None | `None` |
| `release_notes_locale` | str | `"en-US"` |
| `user_fraction` | float \| None | `None` |
| `status` | str \| None | `None` |
| `confirm` | bool | `False` |
| `confirmation_token` | str \| None | `None` |

Releases already-uploaded builds on a track.

`version_codes` are the integer `versionCode` values from your build (e.g.
`["4501"]`), **not** version names like `"3.2.1"`. `status` is one of `draft`,
`inProgress`, `completed`. `user_fraction` is a share between 0 and 1 —
percentages like `10` are rejected.

**Policy:** on the production track a full release is refused. Pass a
`user_fraction` of 0.2 or less (`0.1` is used if omitted), then widen with
`play_expand_rollout`. Use `status="draft"` to stage a production release served
to nobody until you release it.

### `play_promote_release(...)` — gated

| Argument | Type | Default |
|---|---|---|
| `package_name` | str | — |
| `from_track` | str | — |
| `to_track` | str | — |
| `user_fraction` | float \| None | `None` |
| `confirm` | bool | `False` |
| `confirmation_token` | str \| None | `None` |

Promotes the build on one track to another (e.g. beta → production). **The
highest-risk tool in StorePilot.**

Nothing is re-uploaded — the artifact testers already have is what ships. The
preview names the exact build, what the destination track serves today, and how
many users are affected.

**There is no rollback on Play.** Users who update keep the build. The only
remedies are `play_halt_rollout` (stops *new* users receiving it) and shipping a
higher version code.

### `play_expand_rollout(...)` — gated

| Argument | Type | Default |
|---|---|---|
| `package_name` | str | — |
| `user_fraction` | float | — |
| `track` | str | `"production"` |
| `confirm` | bool | `False` |
| `confirmation_token` | str \| None | `None` |

Widens a staged rollout. **The only path to 100% of production users**,
deliberately separated from the release and promote tools so it can never happen
as a side effect.

`user_fraction` is a share between 0 and 1; `1.0` completes the rollout, after
which it can no longer be halted. Rollouts can only grow. Check
`play_get_vitals` before every widening step.

### `play_halt_rollout(package_name, track="production")` — **not gated**

| Argument | Type | Default |
|---|---|---|
| `package_name` | str | — |
| `track` | str | `"production"` |

Stops a staged rollout **immediately**. No preview, no token, no second round
trip — during an incident, a second round trip is the failure. It is still
audited.

**Halting is not a rollback.** It stops new users receiving the build; users who
already updated keep it, and the only fix for them is a corrected build with a
higher version code. A rollout that already reached 100% (`completed`) cannot be
halted. Resume with `play_expand_rollout` once the cause is fixed.

### `play_reply_review(package_name, review_id, text, confirm=False, confirmation_token=None)` — gated

| Argument | Type | Default |
|---|---|---|
| `package_name` | str | — |
| `review_id` | str | — |
| `text` | str | — |
| `confirm` | bool | `False` |
| `confirmation_token` | str \| None | `None` |

Publishes a **public** developer reply. The preview shows the review being
answered and your reply verbatim — show it to the user word for word.

This carries the **full** token gate, like every other write. The review text
being answered is attacker-controlled and reaches the model through
`play_list_reviews`, so "post a reply" is precisely what a prompt injection asks
for; `confirm=True` alone would be a single flag for the model to set on its own.
The reply text itself is never flattened in the preview — the human approves the
exact bytes that go out.

The reply is public on your store listing, is emailed to the reviewer, and
**cannot be deleted — only overwritten**. Google rejects replies over **350
characters** and strips HTML. Never include personal data. Requires the "Reply
to reviews" permission.

### `play_update_listing(...)` — gated

| Argument | Type | Default |
|---|---|---|
| `package_name` | str | — |
| `locale` | str | — |
| `title` | str \| None | `None` |
| `short_description` | str \| None | `None` |
| `full_description` | str \| None | `None` |
| `changes_not_sent_for_review` | bool | `False` |
| `confirm` | bool | `False` |
| `confirmation_token` | str \| None | `None` |

Overwrites store listing copy for one locale. The preview is a before/after diff
of the live listing.

Fields you omit are untouched. Play's limits are enforced **before** anything is
sent — **title 30, short description 80, full description 4000** — because Play
rejects the whole edit if any field is over, so nothing else would apply either.

Each locale is a separate listing: a wrong locale code creates a listing in a
language you did not intend.

`changes_not_sent_for_review=True` saves the change without submitting it for
review. Use it when a release is already in review, because a normal commit
cancels that review and restarts it.

---

## App Store Connect

Every `app` argument accepts a numeric Apple ID **or** a bundle id, and resolves
the bundle id for you.

### `asc_list_apps()`

No arguments. Returns each app's numeric Apple ID and bundle id. Start here — the
Apple ID is what the other tools want.

### `asc_list_builds(app, version=None, processing_state=None, limit=25)`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `app` | str | — | Apple ID or bundle id |
| `version` | str \| None | `None` | Marketing version, e.g. `"3.2.1"` — not the build number |
| `processing_state` | str \| None | `None` | `PROCESSING`, `FAILED`, `INVALID`, `VALID` |
| `limit` | int | `25` | Capped at 200 |

TestFlight builds, newest first, showing the **three separate states** that
decide whether testers can actually install: processing state, internal/external
TestFlight state, and beta review state. Expired builds are marked.

Builds appear only after a successful upload via Xcode, Transporter or `xcrun
altool`. Apple expires them 90 days after upload.

### `asc_list_reviews(app, min_rating=1, max_rating=5, territory=None, only_unanswered=False, limit=25)`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `app` | str | — | Apple ID or bundle id |
| `min_rating` | int | `1` | 1–5 |
| `max_rating` | int | `5` | 1–5 |
| `territory` | str \| None | `None` | ISO 3166-1 **alpha-3** (`"USA"`, `"GBR"`, `"JPN"`); common two-letter codes are translated |
| `only_unanswered` | bool | `False` | The working queue for `asc_reply_review` |
| `limit` | int | `25` | Capped at 200 |

Returns reviews with stars, date, territory, author, reply status, text and
`review_id`.

Unlike Google Play, Apple returns the **full history**, not just the last week,
and **includes reviews with no text**.

Reviews are per-storefront: a territory filter shows only reviews written in that
country's store. With `only_unanswered` the tool over-fetches, and says so when
the unanswered subset came from the newest page rather than the whole history.

### `asc_list_versions(app, platform="IOS", limit=10)`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `app` | str | — | Apple ID or bundle id |
| `platform` | str | `"IOS"` | `IOS`, `MAC_OS`, `TV_OS`, `VISION_OS` |
| `limit` | int | `10` | Capped at 50 |

Versions with their review/release state, attached build, and the phased-release
day plus the share of users reached — Apple's equivalent of a Play staged
rollout. Marks which version is editable, i.e. which one metadata changes target.

### `asc_get_sales(period, frequency="DAILY", app=None, end_period=None, report_type="SALES")`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `period` | str | — | `YYYY-MM-DD` for DAILY/WEEKLY (WEEKLY must be the Sunday **ending** the week), `YYYY-MM` for MONTHLY, `YYYY` for YEARLY |
| `frequency` | str | `"DAILY"` | `DAILY`, `WEEKLY`, `MONTHLY`, `YEARLY` |
| `app` | str \| None | `None` | Omit for the whole account |
| `end_period` | str \| None | `None` | DAILY **and** `report_type="SALES"` only; reads every day inclusive, **capped at 31 days** |
| `report_type` | str | `"SALES"` | `SALES`, `SUBSCRIPTION`, `SUBSCRIPTION_EVENT`, `PRE_ORDER` |

Units and developer proceeds.

**Notes:**

- **Requires `STOREPILOT_ASC_VENDOR_NUMBER`** and a key with the Admin, Finance
  or Sales role.
- Apple rate limits this endpoint far harder than the rest of the API, so results
  are cached — **past periods forever**, since Apple never rewrites them.
  Re-reading a range you already pulled is free.
- For a long span prefer `frequency="MONTHLY"` with a single period over dozens
  of daily requests. `end_period` is rejected with a non-DAILY frequency, and
  with any `report_type` other than `SALES`.
- Revenue is computed as units × per-unit proceeds. Summing Apple's proceeds
  column directly reports one unit's earnings as the total.

### `asc_get_analytics(app, category="APP_USAGE", granularity="DAILY", create=False, max_segments=3)`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `app` | str | — | Apple ID or bundle id |
| `category` | str | `"APP_USAGE"` | `APP_USAGE`, `APP_STORE_ENGAGEMENT`, `COMMERCE`, `FRAMEWORK_USAGE`, `PERFORMANCE` |
| `granularity` | str | `"DAILY"` | `DAILY`, `WEEKLY`, `MONTHLY` |
| `create` | bool | `False` | Register a report request if none exists |
| `max_segments` | int | `3` | Clamped to 1–20 |

Apple's analytics flow is four asynchronous levels deep (request → report →
instance → segment). This tool advances it as far as it can, then returns the
current stage and what to do next. **It never blocks.** Call it again later and
it resumes.

`create=True` starts a clock rather than returning numbers: Apple takes
**24–48 hours** to produce the first data.

### `asc_upload_build(app="", path="")`

Both arguments are unused; they are accepted only so the intent is recorded.

**Apple's REST API has no binary-upload path at all.** This tool exists so an
agent gets a correct answer instead of inventing an endpoint. It returns the
working `xcrun iTMSTransporter` and `xcrun altool` commands (both accept the same
`.p8` key StorePilot uses), plus the Xcode Organizer, Transporter.app and
fastlane alternatives, and notes Apple's ~150 binaries per app per day cap.

### `asc_reply_review(review_id, text, confirm=False, confirmation_token=None)` — gated

| Argument | Type | Default | Notes |
|---|---|---|---|
| `review_id` | str | — | From `asc_list_reviews` |
| `text` | str | — | Published verbatim, publicly. Apple's limit is **5970** characters |
| `confirm` | bool | `False` | |
| `confirmation_token` | str \| None | `None` | |

Public developer response. Apple notifies the reviewer. It can be edited or
withdrawn afterwards, but not un-sent — which is why it is two-step.

### `asc_update_metadata(...)` — gated

| Argument | Type | Default | Apple limit |
|---|---|---|---|
| `app` | str | — | |
| `locale` | str | `"en-US"` | Must already exist on the version |
| `name` | str \| None | `None` | 30 chars |
| `subtitle` | str \| None | `None` | 30 chars |
| `keywords` | str \| None | `None` | 100 chars total, comma-separated, no spaces after commas — separators count |
| `promotional_text` | str \| None | `None` | 170 chars |
| `description` | str \| None | `None` | 4000 chars |
| `whats_new` | str \| None | `None` | 4000 chars |
| `confirm` | bool | `False` | |
| `confirmation_token` | str \| None | `None` | |

Updates listing copy on the **editable** version. Only fields you pass change,
and limits are checked before anything is sent — Apple rejects them at
submission time, which costs a full review cycle.

**Notes:**

- Name and subtitle live on a different Apple resource (`appInfo` localizations)
  from the rest of the copy. This is handled for you, but it is why they can
  fail independently.
- Creating a **new** localization requires the App Store Connect UI. If the
  locale does not exist on the version, the tool lists the ones that do.
- Nothing reaches users until the version is submitted and approved.
  **Promotional text is the exception** — it goes live without a new submission.
- The confirmation token is bound to the resolved version id, not just your
  arguments: if App Store Connect rolls to a new editable version between preview
  and confirm, the token stops working rather than writing somewhere else.

### `asc_submit_for_review(app, platform="IOS", phased_release=True, skip_precheck=False, confirm=False, confirmation_token=None)` — gated

| Argument | Type | Default | Notes |
|---|---|---|---|
| `app` | str | — | |
| `platform` | str | `"IOS"` | `IOS`, `MAC_OS`, `TV_OS` |
| `phased_release` | bool | `True` | Apple's 7-day ladder: 1%, 2%, 5%, 10%, 20%, 50%, 100% |
| `skip_precheck` | bool | `False` | Submit despite precheck problems |
| `confirm` | bool | `False` | |
| `confirmation_token` | str \| None | `None` | |

Submits the editable version to App Review.

A **local precheck runs first** and is a blocking stop, not a preview: if it
finds problems, nothing is submitted and **no token is issued**, because there is
nothing to confirm while the submission would certainly be rejected. It checks
for a valid attached build, non-empty descriptions and release notes, a privacy
policy URL, and every Apple length limit. Overriding requires the separately
named `skip_precheck`.

`phased_release` is on by default because an unphased release reaches every user
at once with no way to slow it down. Once submitted, the version is **frozen** —
metadata cannot be edited until Apple responds, typically 24–48 hours.

---

## Cross-store

These register when either store is configured, and are designed to still answer
with one store missing.

### `portfolio_overview(month="", days=28)`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `month` | str | last complete month | Revenue/installs/rating month |
| `days` | int | `28` | Vitals window |

**Every app on both stores in one table.** Live version and track, staged-rollout
share, rating, installs, revenue with its currency, and crash/ANR against
Google's thresholds. Takes no app id — start here for any portfolio question.

**It degrades rather than failing.** One app's missing permission, or a store
that is not configured at all, shrinks the table instead of emptying it. Every
cell it could not fill carries a reason code with a legend underneath, because a
blank cell is a lie by omission — the reader fills it in with "zero":

| Code | Meaning |
|---|---|
| `off` | store not configured on this machine — run `setup_doctor` |
| `no-store` | this app does not exist on that store |
| `no-api` | the store's API publishes no such figure (not a StorePilot limitation) |
| `no-bucket` | Play reports bucket not configured |
| `no-perm` | authenticated, but this account lacks permission for that data |
| `not-pub` | the store has not published this period yet — **not** zero |
| `no-data` | the store returned no rows for this period |
| `suppressed` | Android Vitals suppresses metrics below a minimum daily user count |
| `quota` | rate limited upstream; this call could not fetch it |
| `no-vendor` | `STOREPILOT_ASC_VENDOR_NUMBER` unset, so Apple sales cannot be read |
| `error` | the call failed — see "Per-app issues" below the table |

It never prints "ok" for something it did not measure: Apple publishes no crash
rate, so App Store rows read `no-vitals`, and Apple publishes no aggregate rating
through its API at all, so that cell reads `no-api`. It never adds two currencies
together.

Apps on both stores are joined only when the pairing is in `apps.toml`. Unpaired
apps still appear, one row per store.

### `compare_reviews(app="", days=30, limit=50)`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `app` | str | `""` | Registry key, name, package or Apple ID. Empty works when only one app is registered |
| `days` | int | `30` | How far back to keep App Store reviews |
| `limit` | int | `50` | Max reviews per store |

A rating-distribution table for the two stores side by side, then the review
texts labelled by store and grouped by rating, so per-platform differences in
what users complain about are readable directly. Retrieval and structuring
happen here; reading the sentiment is left to you.

**The two samples are not comparable as populations, and the output says so.**
Play exposes only production-track, comment-bearing reviews from roughly the last
7 days; Apple returns full history including text-less ratings. The distribution
table therefore describes what each store returned, not each store's rating.

### `parity_check(app="", locale="")`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `app` | str | `""` | Empty checks every paired app |
| `locale` | str | `""` | Spelled as **Play** spells it (`"en-US"`, `"vi"`); the Apple equivalent is derived. Empty uses the app's first registered locale, else `en-US` |

Reports **differences only**: live version, rollout state, and the listing fields
that exist on both stores (title/name, short description/subtitle, full
description/description).

Where a value would not fit if copied to the other store, it says by how much.
Play allows 30/80/4000 for title/short/full; Apple allows 30/30/4000/100/170 for
name/subtitle/description/keywords/promotional text. A rejected Apple submission
costs days.

The stores disagree on some locale codes and the disagreements are not guessable
— Play's `zh-TW` is Apple's `zh-Hant`, and Play still ships Hebrew under the
pre-1989 code as `iw-IL` where Apple uses `he`. The mapping is a table; an
unmapped locale is reported rather than silently approximated.

### `release_both(...)` — gated

| Argument | Type | Default | Notes |
|---|---|---|---|
| `version_name` | str | — | The marketing version both stores show, e.g. `"3.2.1"` |
| `aab_path` | str | `""` | Omit to re-release the build already on the track |
| `release_notes` | str | `""` | Play release notes **and** TestFlight "What to Test" |
| `play_track` | str | `"internal"` | `"production"` forces a ≤20% first step |
| `app` | str | `""` | Registry key of a paired app |
| `testflight_locale` | str | `"en-US"` | |
| `confirm` | bool | `False` | |
| `confirmation_token` | str \| None | `None` | |

Ships one version to Google Play **and** Apple TestFlight.

**One token covers both stores.** Change either store's parameters and it stops
working, because approving half a two-store release is not approving this
release.

On the preview leg the Play half runs for real inside a throwaway edit that is
validated and discarded, and the Apple half is checked against the build actually
sitting in TestFlight.

**Apple has no API that accepts a binary**, so the `.ipa` must already be in
TestFlight. If it is not, the Apple half is reported as **SKIPPED with the exact
upload command** — never as success.

On partial failure the result says exactly what landed where and refuses to call
it a success. **Play is not rolled back**: pulling a working build from users
because the other store returned an API error is the worse outcome.

### `metadata_pull(app="", store="both", locales="", metadata_dir="")`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `app` | str | `""` | Registry key, name, package or Apple ID |
| `store` | str | `"both"` | `"both"`, `"play"`, `"ios"` |
| `locales` | str | `""` | Comma-separated; empty means every locale the store has |
| `metadata_dir` | str | `""` | Defaults to the registry's `metadata_dir`, else `~/.storepilot/metadata/<key>` |

Downloads store listing copy into **fastlane's own directory layout**, so the
same checkout keeps working with `supply` and `deliver` and you can migrate in
either direction:

```
<base>/metadata/android/<locale>/title.txt              (fastlane supply)
                                 short_description.txt
                                 full_description.txt
                                 video.txt
                                 changelogs/<versionCode>.txt
<base>/metadata/ios/<locale>/name.txt                   (fastlane deliver)
                             subtitle.txt
                             description.txt
                             keywords.txt
                             promotional_text.txt
                             release_notes.txt
                             marketing_url.txt
                             support_url.txt
                             privacy_url.txt
```

A file is written only when the store returned a value for that field, so a
listing with no promo video produces no `video.txt`.

One deliberate deviation, called out because it is the only one: `deliver`
defaults to `fastlane/metadata/<locale>` with no platform segment, because it
only ever handled Apple. Putting Apple under `metadata/ios` is what lets both
stores share one tree. Existing fastlane users keep working with a one-line
change: `deliver(metadata_path: "metadata/ios")`. Filenames inside each locale
directory are byte-for-byte fastlane's.

Files whose content already matches are left untouched, so `git status` shows
only real changes.

### `metadata_push(app="", store="both", locales="", metadata_dir="", confirm=False, confirmation_token=None)` — gated

Same arguments as `metadata_pull`, plus the gate. Publishes the local tree to one
or both stores. The preview is a real before/after diff of every field that would
change.

**Fields whose local content is byte-identical to what the store serves are
skipped**, compared by **content digest** rather than file timestamp: a git
checkout or a formatter must never cause a store write. On Play a needless write
can push an app back into review; on Apple it dirties a version that was ready to
submit.

Anything over a store's length limit blocks the **whole** push before a single
request goes out.

### `list_app_pairs()`

No arguments. Shows the pairing registry at `~/.storepilot/apps.toml` (override
with `STOREPILOT_APPS_FILE`).

No store API states that a Play package and an App Store app are the same
product, which is why this file exists. A registry entry that no longer resolves
is **reported, not dropped** — that is either a typo or a permissions gap, and
both need saying out loud.

### `suggest_app_pairs()`

No arguments. Reads both stores' app lists, scores every combination on
bundle-id and name evidence, and proposes a one-to-one matching with the
reasoning for each.

**Nothing is applied.** A proposal is inert until `pair_apps` writes it: a wrong
pair silently attributes one app's revenue, reviews and crash rate to another,
and nothing downstream would look wrong. The file is the trust boundary; the
heuristic only drafts it.

### `pair_apps(key="", play="", appstore="", name="", bundle_id="", metadata_dir="", locales="")`

| Argument | Type | Notes |
|---|---|---|
| `key` | str | Short registry key, e.g. `"acme-todo"`. Derived from the name if omitted |
| `play` | str | Play package name, e.g. `"com.acme.todo"` |
| `appstore` | str | **Numeric Apple ID**, e.g. `"1234567890"` — not the bundle id |
| `name` | str | Display name used in cross-store output |
| `bundle_id` | str | iOS bundle id; helps future auto-pairing |
| `metadata_dir` | str | Where this app's fastlane metadata tree lives |
| `locales` | str | Comma-separated default locales for the metadata tools |

Writes one app into the registry. Passing only one store id is valid and
registers a single-store app. Calling it again for the same app extends the
existing entry rather than creating a duplicate.

---

## The safety model

Full rationale in [SECURITY.md](SECURITY.md).

**Two calls.** The first returns a preview plus a confirmation token; the second
carries the token back with identical arguments and `confirm=true`.

**The preview is the safety mechanism.** It renders into the chat where a human
can read it and notice the wrong app or the wrong track. Play previews are not
estimates: the operation runs inside a throwaway Play edit that is validated and
then deleted, so the preview reports what Google actually said.

**The token only prevents drift.** It is an HMAC over a canonical fingerprint of
the operation, keyed with a per-install secret that never appears in tool output,
single-use, and valid for **10 minutes**. A plain content hash would be
computable by the model itself, letting it self-confirm without ever rendering
the preview a human needs to see. If the key file cannot be persisted, StorePilot
falls back to a process-lifetime key — every stale token is rejected, which fails
closed.

A rejected token means one of: missing, malformed, expired, already spent, or
bound to different arguments. Re-run with `confirm=false` for a fresh preview.

**Rollout policy.** Production is forced into a staged rollout capped at 20% on
the first step (`STOREPILOT_MAX_INITIAL_ROLLOUT`, clamped to 0.01–0.5 so it can
be tightened but never disabled). `play_expand_rollout` is the only path to 100%.

**Audit.** Every write attempted — previewed, confirmed, rejected, executed,
failed — is appended to `~/.storepilot/audit.log`. Credentials are never written
to it and long values are stored as a prefix plus a digest, so the log stays
greppable while still proving which text was published. If the log cannot be
written, tools still work but carry a degraded-bookkeeping banner.

**Tool annotations.** Every tool carries MCP annotations so clients can decide
when to stop and ask a human. Write tools are annotated destructive **even on the
preview leg**, because the same tool can mutate a live app on a later call —
annotate by worst case.
