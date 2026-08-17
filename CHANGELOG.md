# Changelog

## 0.1.1

Adds the ownership marker the MCP Registry requires in the PyPI README, so the
server can be listed. No code changes.

## 0.1.0

First release. 34 tools across Google Play, App Store Connect and both at once.

**Google Play** — vitals against Google's crash and ANR thresholds, Google's own
anomaly detections, installs, ratings and earnings from the private reports
bucket, reviews, and a portfolio-wide health scan. Guarded writes for bundle
upload, releases, promotion, rollout expansion, halt, review replies and listing
copy.

**App Store Connect** — apps, TestFlight builds, customer reviews, versions,
sales and the asynchronous analytics chain. Guarded writes for review replies,
metadata and review submission.

**Cross-store** — `portfolio_overview` puts every app from both stores in one
table, plus review comparison, version and listing parity, a one-token release to
both stores, and a fastlane-layout metadata mirror.

**Safety** — every write previews first and requires an HMAC-keyed, single-use,
content-bound confirmation token. Production releases are forced into a staged
rollout capped at 20%. `play_halt_rollout` is deliberately ungated. Everything is
audited.

### Fixed before release, from the first live runs

The first read-only runs against real accounts on both stores found three things
no fixture could have:

- Apple rejects `sort` on the `appStoreVersions` relationship endpoint, which
  made `asc_list_versions` fail outright. Ordering is now client-side and
  compares version components numerically, so `1.10.0` sorts above `1.9.0`.
- The Play reports bucket is not always `pubsite_prod_rev_*`; a live account
  returned `pubsite_prod_<accountId>`. The parser already accepted both, but the
  documentation would have led users to think they had copied the wrong URI.
- Bucket access is an account-level Play Console permission. The 403 remedy used
  to send users to Cloud Console IAM, for a bucket in a Google-owned project they
  cannot administer.

### Fixed before release, from review

- Production ignored an explicit `status="halted"` and returned a live rollout at
  the default 10%.
- Every tool handed the model bare parameter names. The SDK does not read an
  `Args:` docstring section, so tools that looked documented in source exposed
  `user_fraction`, `status` and `track` with no description at all.

### Security fixes found by audit

- `play_reply_review` published on `confirm=True` alone, with no token and no
  preview. A Play reply cannot be deleted, only overwritten.
- Reviewer display names were not sanitized, so a review could forge StorePilot's
  own output and instruct the model to publish a reply.
- `play_halt_rollout` wrote back only the halted release, silently unassigning the
  build still serving the rest of the users.
- `play_promote_release` bound its token to track names only, so a build that
  changed between preview and confirm shipped under an approval for a different
  version.
- A `NaN` rollout ceiling disabled the staged-rollout policy entirely.
- State files holding revenue data and the write history were world-readable.
