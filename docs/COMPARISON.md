# How StorePilot compares

StorePilot is not the first MCP server for these APIs, and it is not a fastlane
replacement. This page is specific about what the alternatives do better and
about the narrow band where StorePilot is currently the only option.

Repository data below was read from the GitHub API on **17 August 2026**, and the
tool counts from each project's own README on the same day. Star counts are a
popularity proxy, not a quality measure, and they move. Verify before relying on
them.

---

## The landscape

| Project | Stars | Last push | Stores | Shape |
|---|---:|---|---|---|
| [blitzdotdev/blitz-mac](https://github.com/blitzdotdev/blitz-mac) | 1,739 | 2026-07-14 | App Store | Native macOS app with an MCP interface |
| [JoshuaRileyDev/app-store-connect-mcp-server](https://github.com/JoshuaRileyDev/app-store-connect-mcp-server) | 330 | 2025-09-02 | App Store | MCP server — **archived** |
| [antoniolg/play-store-mcp](https://github.com/antoniolg/play-store-mcp) | 155 | 2025-10-03 | Google Play | MCP server, 3 deploy-focused tools |
| [appreply-co/mcp-appstore](https://github.com/appreply-co/mcp-appstore) | 67 | 2026-08-03 | Both (public data) | ASO research over public store listings |
| [erayendes/app-store-connect-mcp](https://github.com/erayendes/app-store-connect-mcp) ("Heimdall") | 46 | 2026-08-13 | App Store | 883 tools generated from Apple's OpenAPI spec, 13 profiles |
| [mikusnuz/app-publish-mcp](https://github.com/mikusnuz/app-publish-mcp) | 26 | 2026-08-06 | **Both** | 115 tools across ASC + Play Console |
| **StorePilot** | — | — | **Both** | 34 portfolio-shaped tools |

Two clarifications, because they are easy to get wrong:

- **StorePilot is not the only both-stores MCP server.** `app-publish-mcp` covers
  App Store Connect and Google Play Console in one server and predates this one.
- **`mcp-appstore` is a different product category.** It reads *public* store
  listings for ASO research. It does not touch your developer account, so it is
  complementary rather than competing.

---

## What StorePilot does that the others do not

Each of these is checkable against the source of both projects.

### Play installs, ratings and earnings

Those figures have no Play REST API. They exist only as UTF-16 CSVs in a private
Cloud Storage bucket that Google writes for your developer account. Reading them
means a third OAuth scope, an account-level Play Console permission that is
separate from every per-app grant, and a CSV parser that resolves columns by name
because the headers change without notice.

None of the servers above read that bucket, so "how much did this app earn last
month?" is unanswerable in them. It is the single largest capability gap.

### Android Vitals with Google's thresholds

The Play Developer Reporting API is a separate product from Android Publisher,
with its own OAuth scope and its own enablement. StorePilot uses it for
user-perceived, user-weighted crash and ANR rates and states a verdict against
Google's bad-behaviour thresholds (1.09% / 0.47%), plus Google's own anomaly
detections. None of the servers above call it.

### Portfolio-shaped tools

`portfolio_overview` and `play_portfolio_health` take **no app id**. Every other
server here is endpoint-shaped: one call, one app, one resource. That is the
right design for scripting a single release and the wrong one for answering
"which of my thirty apps is in trouble?", which otherwise costs thirty tool calls
and a lot of context.

### Cross-store operations

`parity_check` (listing and version drift between stores), `compare_reviews`
(both stores' reviews side by side), `release_both` (one version, one token, both
stores), and a fastlane-layout metadata mirror. A server that covers both stores
in the same process does not automatically get these — they require a pairing
registry, a locale-code mapping table, and per-store length validation.

### Guards that are on by default

Heimdall also offers confirm-before-write, so the *idea* is not unique. The
specifics differ: StorePilot's gate is always on, the confirmation token is an
HMAC over a canonical fingerprint of the operation (single-use, 10-minute TTL,
keyed with a secret the model never sees), and Play previews execute for real
inside a throwaway edit that is validated and deleted, so the preview reports
what Google actually said rather than an estimate. Production releases are
additionally forced into a staged rollout capped at 20%.

The threat model is written for a caller that hallucinates tokens, reuses old
ones, and adjusts a parameter between preview and confirmation. See
[SECURITY.md](SECURITY.md).

### `setup_doctor`

Credential setup spans Google Cloud, two separate tabs of Play Console
permissions and App Store Connect, and several of its failures are silent — most
importantly Play's
`reviews.list` returning HTTP 200 and an empty list when the "Reply to reviews"
permission is missing. Every project here documents its setup in a README;
StorePilot also diagnoses it at runtime and prints the specific fix per step.

`app-publish-mcp` documents the "Reply to reviews" trap in its README, which is
more than most.

---

## What the others do better

### Heimdall — Apple API breadth

883 tools generated from Apple's official OpenAPI spec, organised into 13
profiles and 32 sub-profiles you can narrow to, covering subscriptions and
pricing, Game Center, Xcode Cloud, provisioning, webhooks, and sales and finance
reports. StorePilot exposes 10 Apple tools.

If your work is deep inside App Store Connect — subscription price matrices,
offer codes, Xcode Cloud — Heimdall covers surface StorePilot does not, and its
profile system is a genuinely better answer to tool-definition token cost than a
hand-curated set. It is also actively maintained and stores the key in the
macOS Keychain.

### app-publish-mcp — publishing surface

115 tools — 70 App Store Connect, 45 Google Play — including in-app purchases and
subscriptions on both stores, screenshot set creation and upload, certificates,
provisioning profiles and device registration, TestFlight tester and beta-group
management, age ratings, and pricing.

StorePilot does **none** of that. If you need to upload screenshots or manage
IAPs conversationally, this is the server that does it today.

### blitz-mac — reach and packaging

A native macOS application rather than a stdio server you configure by hand, and
by a wide margin the largest community here. If you want a GUI with an MCP
interface attached rather than a server, that is a different and reasonable
product shape.

### antoniolg/play-store-mcp — simplicity

Three tools: deploy, promote, get releases. If that is genuinely all you need,
it is far less to install, configure and reason about than StorePilot. Note it
describes itself as under development and its last push was October 2025.

### JoshuaRileyDev/app-store-connect-mcp-server

Still the most-starred dedicated ASC MCP server, but the repository is
**archived** and was last pushed in September 2025. Included here because its
star count still makes it the first search result many people find.

---

## StorePilot and fastlane

**fastlane owns CI-driven release automation, and it is excellent at it.**
StorePilot does not compete with it and is not trying to replace it.

| | fastlane | StorePilot |
|---|---|---|
| Runs in | CI, scripted lanes | An MCP client, conversationally |
| Trigger | A commit, a tag, a cron | A question a human asked |
| Code signing | `match`, `sigh`, `cert` | Not attempted |
| Screenshots | `snapshot`, `frameit` | Not attempted |
| Build & upload | `gym`, `supply`, `pilot`, `deliver` | Play upload only; iOS defers to Transporter |
| Installs / earnings | Not its job | Primary capability |
| Android Vitals | Not its job | Primary capability |
| "Which app is in trouble?" | Not its job | Primary capability |
| Maturity | Years of production use | Read paths run live once; no write ever has |

The two answer different questions. fastlane answers "build and ship this commit,
reproducibly, without a human." StorePilot answers "how is the portfolio doing,
and what needs attention" — questions asked in the middle of a conversation,
where the cost of the answer is measured in how many steps it took to get it.

**They are designed to coexist.** `metadata_pull` and `metadata_push` write and
read fastlane's own directory layout — `metadata/android/<locale>/title.txt` for
`supply`, `metadata/ios/<locale>/name.txt` for `deliver` — with byte-identical
filenames. There is exactly one deviation, the `ios` platform segment that lets
both stores share one tree, and existing `deliver` users absorb it with a
one-line change:

```ruby
deliver(metadata_path: "metadata/ios")
```

So the same checkout can be edited by a human, pushed by StorePilot after a
previewed diff, and shipped by fastlane in CI. Adopting one is not abandoning
the other, which was a deliberate design constraint rather than a coincidence.

If your release process is already a green fastlane pipeline, keep it. StorePilot
is for the questions that pipeline does not answer.

---

## When not to use StorePilot

Honestly:

- **You need it to work today, with certainty.** The read tools have run against
  real accounts on both stores; no write ever has, and neither the earnings
  parser nor the vitals thresholds have seen real data. Use fastlane or a mature
  server, and file bugs here.
- **You have one app.** The portfolio tools are the point, and one app does not
  need them.
- **You need IAPs, subscriptions, pricing, screenshots or certificates.** Not
  implemented. `app-publish-mcp` or Heimdall.
- **You need deep App Store Connect surface.** Heimdall exposes 883 Apple tools
  to StorePilot's 10.
- **You want a GUI.** `blitz-mac`.

Corrections to anything on this page are welcome as issues — particularly from
maintainers of the projects listed, if StorePilot has described their work
inaccurately.
