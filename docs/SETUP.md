# Setup

Credential setup is the hardest part of using either store's API, and most of
the failure modes are silent. This page walks both stores in the order you
actually do them. **Every step ends with a `setup_doctor` check**, so you always
know whether the last thing you did worked before moving on.

Ask your assistant to "run setup_doctor" at any point. It runs every step
independently and reports all of them, so one failure never hides the rest.

Do the stores in either order. You can stop after one — each adapter activates
on its own, and the cross-store tools register as soon as either store works.

---

## Before you start

```bash
git clone https://github.com/sonlenef/storepilot-mcp
cd storepilot-mcp
python -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env
```

Configuration comes from environment variables (prefix `STOREPILOT_`) or from a
`.env` file in the working directory. If you configure the server through your
MCP client's `env` block instead, the same names apply.

**Check:** run `setup_doctor`. Everything should read `[skip]` or `[fail]` with
"not configured". That is the expected starting point — it proves the server
runs.

---

## Google Play

Five things must all be true, and they fail in different places: Google Cloud
(APIs and IAM), Play Console (permissions), and the reports bucket (IAM again,
not Play Console).

### Step 1 — Google Cloud project and a service account

1. Open [Play Console → Setup → API access](https://play.google.com/console) and
   link a Google Cloud project, or create one. This is what ties the two systems
   together.
2. In [Google Cloud Console](https://console.cloud.google.com) with that project
   selected, enable **both** APIs. They are separate products and enabling one
   does not enable the other:
   - **Google Play Android Developer API** (`androidpublisher.googleapis.com`)
   - **Google Play Developer Reporting API**
     (`playdeveloperreporting.googleapis.com`)
3. **IAM & Admin → Service Accounts → Create service account.** No project roles
   are needed at this stage.
4. On the new account: **Keys → Add key → Create new key → JSON**. Download it.
   Google does not let you download it again.
5. Point StorePilot at it:

   ```
   STOREPILOT_GOOGLE_CREDENTIALS=/absolute/path/to/service-account.json
   ```

StorePilot requests three OAuth scopes with this key —
`androidpublisher`, `playdeveloperreporting` and `devstorage.read_only` — which
is why the Reporting API has to be enabled separately from Android Publisher.

**Check:** `setup_doctor`. The **Credentials** step should go `[ok]` and print
the service account email:

```
[ok] Credentials: service account storepilot@my-project.iam.gserviceaccount.com loaded
     from /path/to/service-account.json (cloud project: my-project)
     Note: Grant this email access in Play Console -> Users and permissions if you have
     not already: storepilot@my-project.iam.gserviceaccount.com
```

Copy that email. The next step needs it.

### Step 2 — Invite the service account in Play Console

Play Console → **Users and permissions** → **Invite new users** → paste the
service account email from Step 1.

Grant these **app permissions**, spelled exactly as Play Console spells them:

| Permission | Needed for |
|---|---|
| **View app information and download bulk reports (read-only)** | `play_list_apps`, vitals, anomalies, everything read |
| **View financial data, orders, and cancellation survey responses** | `play_get_earnings` |
| **Reply to reviews** | `play_list_reviews` **and** `play_reply_review` |
| **Release apps to testing tracks** / **Release to production…** | the write tools, per track |

Permission changes can take a few minutes to apply.

#### The "Reply to reviews" trap

Read this even if you never intend to reply to a review.

Without **Reply to reviews**, Play's `reviews.list` does not return an error. It
returns **HTTP 200 with an empty list** — the identical response to an app that
genuinely has no reviews. There is nothing to catch, so a tool that trusts the
API will tell you your app has no reviews, forever, and be wrong.

`setup_doctor` treats that ambiguity as its own diagnosis:

```
[warn] Reviews permission: reviews.list returned an EMPTY list for com.example.app —
       this is ambiguous
       Fix: ... enable 'Reply to reviews' ...
```

`play_list_reviews` says the same thing rather than claiming there are no
reviews. Grant the permission and the ambiguity disappears.

**Check:** `setup_doctor`. You want four green lines:

```
[ok] Android Publisher API: reachable, queried com.example.app
[ok] Play Developer Reporting API: reachable — 12 app(s) visible: com.example.app, ...
[ok] Reviews permission: granted — 5 review(s) read from com.example.app
```

If **Play Developer Reporting API** says "reachable, but it reports zero
accessible apps", note the second cause: the Reporting API only lists apps that
have **at least one published release**. A draft app is invisible there, and
that is a Google limitation, not a permission problem.

If either API step fails with "not enabled", that is a Google Cloud setting from
Step 1 — no permission granted in Play Console can fix it, and `setup_doctor`
prints the direct enable link for your project.

### Step 3 — The reports bucket (installs, ratings, earnings)

Installs, ratings and earnings have **no Play REST API**. They exist only as
UTF-16 CSV files in a private Cloud Storage bucket that Google writes for your
developer account. Without this step, vitals and reviews work and every money or
install figure reads "n/a".

1. Play Console → **Download reports** → **Statistics** (or **Financial
   reports**).
2. Click **Copy Cloud Storage URI**. It looks like
   `gs://pubsite_prod_rev_0123456789` on older accounts, or
   `gs://pubsite_prod_<accountId>` on newer ones — both are valid, and StorePilot
   accepts the whole URI including any trailing path.
3. Set the bucket id, without the `gs://` prefix (StorePilot strips it either
   way):

   ```
   STOREPILOT_GOOGLE_REPORTS_BUCKET=pubsite_prod_rev_0123456789
   ```

4. **Grant bucket access in Google Cloud, not Play Console.** This is the step
   everyone misses. Google Cloud Console → **Cloud Storage** → that bucket →
   **Permissions** → grant the service account email the **Storage Object
   Viewer** role.

A 403 on this bucket is an IAM problem that no Play Console permission can fix,
so StorePilot gives it a distinct remedy rather than folding it into "permission
denied".

**Check:** `setup_doctor`.

```
[ok] Reports bucket: gs://pubsite_prod_rev_0123456789 readable
     (stats/installs/installs_com.example.app_202607_overview.csv and others found)
```

"readable but empty" is usually timing, not permissions: reports land 3–7 days
late, and a month's earnings appear around the 5th of the following month.

---

## App Store Connect

Shorter, but the role you pick decides which tools work, and the key file is
downloadable exactly once.

### Step 4 — Create an API key

1. App Store Connect → **Users and Access** → **Integrations** → **App Store
   Connect API**.
2. Create a **team key**. Choose the role by what you need:

   | Role | Gets you |
   |---|---|
   | Developer / App Manager | apps, builds, reviews, versions |
   | **App Manager or Admin** | replying to reviews, submitting for review |
   | **Admin, Finance or Sales** | `asc_get_sales` — sales and finance reports |

   Only an Account Holder or Admin can change a key's role afterwards, and a new
   role only applies to newly minted tokens.
3. Download the `.p8` file. **Apple lets you download it exactly once.**
4. Note two identifiers from that page: the **Key ID** (the column next to your
   key) and the **Issuer ID** (a UUID shown *above* the key table, shared by the
   whole team).
5. Set all three. The adapter stays off until all three are present:

   ```
   STOREPILOT_ASC_KEY_PATH=/absolute/path/to/AuthKey_XXXXXXXXXX.p8
   STOREPILOT_ASC_KEY_ID=XXXXXXXXXX
   STOREPILOT_ASC_ISSUER_ID=00000000-0000-0000-0000-000000000000
   ```

StorePilot signs an ES256 JWT with that key. Apple rejects any token whose
lifetime exceeds 20 minutes, so tokens are minted for exactly that and refreshed
after 15, leaving margin for clock skew — a long MCP session never carries a
dead token.

**Check:** `setup_doctor`. Three steps should turn green:

```
[ok] ASC credentials: loaded key XXXXXXXXXX (issuer 0000...) from /path/AuthKey_XXXXXXXXXX.p8
[ok] ASC token: ES256 token minted (kid XXXXXXXXXX, aud appstoreconnect-v1,
     valid 1200s, auto-refreshed after 900s)
[ok] ASC API reachable: GET /v1/apps returned 4 app(s): My App (com.example.app), ...
```

If **ASC API reachable** returns zero apps, the key works but sees nothing:
confirm its role includes app access and that it belongs to the team that owns
the apps. App records must already exist — no API can create them.

### Step 5 — The vendor number (sales reports only)

`asc_get_sales` needs a vendor number, which is not the Team ID and not the
Issuer ID.

1. App Store Connect → **Payments and Financial Reports**.
2. Read the 8-digit number next to your team name.
3. Set it:

   ```
   STOREPILOT_ASC_VENDOR_NUMBER=80000000
   ```

**Check:** `setup_doctor`.

```
[ok] ASC sales access: vendor number 80000000 configured (not probed —
     /v1/salesReports is rate limited to a few hundred calls a day)
     Note: Run asc_get_sales for a past date to confirm the number and role are right.
```

`setup_doctor` deliberately does **not** probe this endpoint. Apple rate limits
`salesReports` far harder than the rest of the API, and spending that budget on a
diagnostic would be a poor trade. Confirm it for real by calling `asc_get_sales`
for a past date.

---

## Step 6 — Pair your apps

No API on either side says that `com.acme.todo` on Play and Apple ID
`1234567890` are the same product. Cross-store tools need that mapping, so it
lives in a file you own: `~/.storepilot/apps.toml` (override with
`STOREPILOT_APPS_FILE`).

For a portfolio of any size, do not hand-write it:

1. `suggest_app_pairs` reads both stores' app lists, scores every combination on
   bundle-id and name evidence, and proposes a one-to-one matching with its
   reasoning. **Nothing is applied** — a proposal is inert. A wrong pair would
   silently attribute one app's revenue, reviews and crash rate to another, and
   nothing downstream would look wrong.
2. `pair_apps` writes the ones you agree with.
3. `list_app_pairs` shows the result.

Single-store apps are first-class. Registering only a `play` id or only an
`appstore` id is valid, and the app still appears in `portfolio_overview` with
its other-store cells marked "not on this store".

```toml
[apps.acme-todo]
name = "Acme Todo"
play = "com.acme.todo"
appstore = "1234567890"
bundle_id = "com.acme.todo"
metadata_dir = "~/code/acme-todo/fastlane"
locales = ["en-US", "vi"]
```

**Check:** `portfolio_overview`. Every app you own should appear, paired apps on
one row.

---

## Optional configuration

All of these have working defaults. See [`.env.example`](../.env.example).

| Variable | Default | Why you would change it |
|---|---|---|
| `STOREPILOT_CACHE_DIR` | `~/.storepilot/cache` | Report cache location |
| `STOREPILOT_CACHE_ENABLED` | `true` | Set false to always re-download |
| `STOREPILOT_STATE_DIR` | `~/.storepilot` | Sandbox an install per project — moves the guard key, token ledger, audit log and `apps.toml` together |
| `STOREPILOT_AUDIT_LOG` | `<state dir>/audit.log` | Send the write audit trail elsewhere |
| `STOREPILOT_MAX_INITIAL_ROLLOUT` | `0.2` | Tighten the first production rollout step. Clamped to 0.01–0.5, so it can be tightened but never disabled |
| `STOREPILOT_APPS_FILE` | `<state dir>/apps.toml` | Keep the pairing registry in a repo |

---

## Troubleshooting

**A tool returned nothing.** Run `setup_doctor` before anything else. Empty
results are ambiguous on both stores, and it is built to disambiguate them.

**"No apps are visible."** On Play, either the service account was never invited
in Users and permissions, or the apps have no published release (the Reporting
API only lists published apps). On Apple, check the key's role and team.

**Earnings show zero for last month.** If today is before roughly the 5th, the
report does not exist yet. StorePilot says "not published" rather than reporting
0 — those are different facts.

**Installs and revenue read `no-bucket`.** Step 3 is incomplete. The bucket id
and the Storage Object Viewer IAM grant are two separate things and both are
required.

**Vitals read `suppressed`.** Google suppresses Android Vitals entirely for apps
below a minimum daily user count. For a low-traffic app, "no data" does not mean
"no crashes".

**A confirmation token was rejected.** Tokens are single-use, expire after 10
minutes, and are bound to the exact arguments of the preview that issued them.
Re-run the tool with `confirm=false` to get a fresh preview. This is working as
designed: a changed argument means the human approved something else.
