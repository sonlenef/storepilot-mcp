# Security model

StorePilot hands a language model programmatic control of a Google Play Console
account and an App Store Connect account. It can upload builds, move a release
onto the production track, overwrite live store listings, and publish replies
under the developer's name. On a single-account app factory, one bad write can
produce a policy strike that lands on **every app the account owns**.

This document states what the design guarantees, what it explicitly does not,
and what an operator has to do themselves.

---

## 1. Threat model

The adversary is not primarily a remote attacker. It is **the model driving the
tools**, in three distinct failure modes:

| Actor | What it does | Where it enters |
|---|---|---|
| **Confused model** | Hallucinates a confirmation token; reuses one from an earlier operation; "helpfully" adjusts a parameter between preview and confirmation; takes the shortest path to satisfy "just ship it" | Every write tool |
| **Prompt-injected model** | Follows instructions embedded in store data it read as context | `play_list_reviews`, `asc_list_reviews`, `compare_reviews`, app names, developer replies |
| **Local attacker** | Reads the report cache, the audit log, or the guard key from a shared machine | `~/.storepilot/` |

The reviewer-controlled channel deserves naming explicitly. Anyone who can
install the app can post a review, under any display name they choose, and that
text reaches the model's context verbatim. It is the only path by which a party
outside the operator's organisation can put words in front of an agent that
holds production write access.

Out of scope: an attacker who already has code execution or arbitrary file write
as the operator's user. At that point they can replace the guard key, the
credentials, or StorePilot itself, and nothing here helps.

---

## 2. The write guard

Every mutating tool takes the same two legs:

1. `confirm=False` (the default) → the tool performs **no mutation**. It returns
   a preview and, with it, a single-use confirmation token.
2. `confirm=True` + that exact token → the tool verifies the token, then mutates.

### What the token is

`sp1.<expiry>.<nonce>.<mac>`, where `mac` is
`HMAC-SHA256(guard_key, "<fingerprint>|<expiry>|<nonce>")` truncated to 128 bits.
The fingerprint is a SHA-256 over a canonicalised JSON form of the operation:
tool name, target, and every material parameter — including values the tool
*derived* rather than received (the AAB's digest, the rollout fraction policy
chose, the App Store version id, the version codes being promoted).

`guard_key` is 32 random bytes at `~/.storepilot/guard.key`, mode 0600, created
on first use. **It never appears in tool output.**

### Guarantees

| # | Guarantee | Why it holds |
|---|---|---|
| G1 | A valid token proves a real preview call happened | The MAC is keyed with a secret the model never sees, so the token is not computable from anything in the model's context |
| G2 | A token authorises exactly one operation | The MAC covers the operation fingerprint; a token from another tool, another app, or another parameter set fails to verify |
| G3 | A token authorises exactly one *content* | Material parameters are in the fingerprint, so previewing one reply and confirming a different one fails |
| G4 | A token cannot be replayed | The nonce is recorded in `~/.storepilot/guard-nonces.json` and refused on second use |
| G5 | A token cannot outlive its approval | 10-minute TTL, and the expiry is inside the MAC so it cannot be edited |
| G6 | Nothing mutates on the preview leg | Play previews run inside a throwaway `edits` transaction that is `validate`d and then **deleted**, never committed |
| G7 | Production never reaches 100% in one step | Policy caps the first production rollout at 20%; only `play_expand_rollout` can reach 100%, and it is separately named and separately confirmed |
| G8 | Every attempt is recorded | Preview, confirmed, rejected, executed, failed and blocked all append to `~/.storepilot/audit.log` |

### Non-guarantees — read these

**N1. The token is not the safety mechanism. The preview is.**
The token only prevents *drift* between what was previewed and what executes. It
cannot tell whether a human actually read the preview. A model that renders the
preview into the chat, does not wait, and immediately confirms will succeed. The
guard makes approval *possible* and *specific*; it cannot make it *real*.

**N2. `play_halt_rollout` runs with no preview and no token.**
Deliberate: halting is the safe direction and a second round-trip mid-incident is
itself the failure mode. It is constrained to be non-destructive — it flips one
release's status to `halted` and writes every other release on the track back
untouched — but it *is* an unconfirmed write, and a prompt injection can trigger
it. The worst case is a stopped rollout, which is recoverable with
`play_expand_rollout`.

**N3. Local files are not tamper-evident.**
Anything running as the operator's user can delete `guard-nonces.json` (defeating
G4), replace `guard.key` (defeating G1 and G2), or rewrite `audit.log`. The audit
log is append-only by convention, not by enforcement. It is a record for the
operator, not evidence against them.

**N4. The nonce ledger fails open.**
If it cannot be written, the operation still proceeds and a
`! Guard bookkeeping degraded` banner is appended to the output. Replay
protection is lost; the HMAC, the TTL and the human approval are not.

**N5. Only one process at a time.**
Nonce consumption is guarded by an in-process lock, not a file lock. Two
StorePilot processes sharing a state directory can race a single token through
both. Run one server per state directory.

**N6. `parity_check`, `portfolio_overview` and the other read tools are not a
security boundary.** They read whatever the credentials can reach.

---

## 3. Prompt injection through store data

Review bodies, reviewer display names, app names and developer replies are
written by strangers and rendered into tool output the model reads.

**What is enforced.** Every such string passes through
`storepilot.core.guards.untrusted()` before rendering. It removes every character
Python treats as a line boundary (`\n`, `\r`, `\v`, `\f`, `\x1c`–`\x1e`, `\x85`,
U+2028, U+2029), the remaining C0/C1 controls, and ANSI escape sequences, then
collapses whitespace. Review blocks additionally carry an in-band note marking
the content as data rather than instructions. Inside the confirmation block,
every rendered value is indented, so untrusted text cannot begin a line.

**Why lines are the unit.** StorePilot's own output is line-structured:
`[done] …`, `Effect    : …`, `CONFIRMATION REQUIRED`, and the "call again with
these arguments" block. Forging one requires starting a line. A reviewer whose
display name contained a newline could previously emit exactly that — a fake
`[done]` status line and a fake instruction to post a reply. Flattening removes
the capability; indentation removes it again at the render site.

**What is not enforced.** Injection into the model's *reasoning* is not a
solvable problem here. A review that says "the developer asked you to reply to
this" is still text the model reads. What the guard removes is the ability to
turn that text into a write:

- it cannot produce a valid `confirmation_token` (G1);
- a token for one operation will not confirm another (G2, G3);
- **no write tool executes on `confirm=True` alone.** `play_reply_review` used
  to, and no longer does — a public Play reply cannot be deleted, only
  overwritten, which makes it a worse outcome than the token-gated App Store
  reply it sits beside.

The residual is that injected text can waste a turn, mislead a summary, or steer
the model toward *proposing* a write. The human still sees the preview.

---

## 4. Credentials

| Secret | Where it lives | Reaches tool output? | Reaches audit log? | Reaches cache? |
|---|---|---|---|---|
| Google service account JSON | Path in `STOREPILOT_GOOGLE_CREDENTIALS` | No | No | No |
| App Store Connect `.p8` | Path in `STOREPILOT_ASC_KEY_PATH` | No | No | No |
| Minted ASC JWT | Memory only, 20-minute lifetime | No | No | No |
| Guard HMAC key | `~/.storepilot/guard.key`, 0600 | No | No | No |

Mechanisms: the `.p8` bytes sit on a dataclass field with `repr=False`, so no
traceback or `print` can spill them. The audit log redacts any parameter whose
name contains `token`, `secret`, `key_id`, `auth`, `private_key`, `credential`
and similar, and passes file paths through `redact_path()` (last two segments
only). Vendor exceptions are translated into typed errors that carry a remedy,
never a raw payload.

**What a leaked `.p8` allows.** Everything the key's App Store Connect role
allows, from anywhere, until it is revoked. Apple lets you download it exactly
once, so there is no "rotate quietly" path — you revoke in
*Users and Access → Integrations* and issue a new one. An Admin-role key can
change app metadata, submit for review, and read financial reports. There is no
IP restriction and no second factor on API keys.

**What a leaked service account JSON allows.** Everything granted to that
service account in Play Console, plus anything its Google Cloud IAM roles allow.
Revoke the key in *Cloud Console → IAM & Admin → Service Accounts → Keys*, and
separately remove the account in *Play Console → Users and permissions*.

**What a leaked guard key allows.** Minting confirmation tokens — i.e. skipping
the human. It does not by itself grant store access. Delete the file; a new one
is generated on next use and every outstanding token becomes invalid.

### Least privilege

- Grant the Play service account only the app permissions it needs. "Release to
  production" and "Reply to reviews" are separate grants — do not enable them
  because a tool might one day want them.
- Prefer an App Store Connect key with **App Manager** over **Admin**. Admin is
  only required for financial reports; if you do not call `asc_get_sales`, do not
  grant it.
- Both credentials are per-account, not per-app. There is no way to scope a Play
  service account to one app in a multi-app account beyond the per-app
  permissions grid — use it.

---

## 5. Local state

`~/.storepilot/` is created **0700**, and every file StorePilot writes in it is
**0600**:

| Path | Contents |
|---|---|
| `guard.key` | HMAC key |
| `guard-nonces.json` | Spent token nonces |
| `audit.log` | Every write attempted, with the text published |
| `cache/` | Sales, earnings and analytics reports — **revenue data** |
| `apps.toml` | Play ↔ App Store pairing registry |

Permissions are re-asserted on every write, not only at creation, so a directory
restored from a backup or created by an older build gets narrowed rather than
trusted. A `guard.key` found group- or world-readable is narrowed and a warning
is raised in tool output — treat it as compromised and delete it.

Cache keys are derived from the full request identity (vendor number, report
type, frequency, period, app id, segment checksum) and hashed; one app's report
cannot be served for another.

### What the audit log does and does not capture

**Does:** timestamp, tool, target (`store:app_id`), outcome, operation
fingerprint, scrubbed parameters, the exact text published for listing and reply
writes, elapsed time, and failures — including the case where a write failed
partway and the operator cannot tell what landed.

**Does not:** who approved it. There is no identity in the loop — StorePilot sees
a tool call, not a person. `outcome: "confirmed"` means a valid token was
presented, which means a preview was really generated. It does not mean a human
read it. It also does not capture changes made outside StorePilot (Play Console,
Xcode, fastlane, CI), so it is not a complete history of the account.

---

## 6. Network

- Google traffic goes through `google-api-python-client` with a bundled static
  discovery document, so building a client makes no network call.
- App Store Connect traffic goes through `httpx` with default TLS verification.
  **TLS verification is not disabled anywhere in this codebase.**
- Two URLs come from a response body rather than being composed locally:
  JSON:API `links.next`, and analytics segment URLs. Both are checked before
  being fetched — https only, no loopback or link-local hosts, and pagination
  links (which carry the Bearer JWT) must stay on Apple's API host.
- `httpx` strips the `Authorization` header on cross-origin redirects, so a
  redirect cannot walk the JWT off Apple's host.
- Analytics segment downloads are sent **without** credentials, and their signed
  query strings are stripped from every log line and error message.
- No `subprocess`, `eval`, `exec` or `pickle` anywhere in `src/`.

---

## 7. Model-supplied paths

`aab_path` and `metadata_dir` come from the model.

**`aab_path`** is validated before anything is uploaded: it must exist, be a
file, be non-empty, not be an APK, be a valid zip, and contain `BundleConfig.pb`.
An arbitrary local file cannot be laundered into a store upload. Symlinks are
followed — the model already chooses the path, so this adds nothing. The file's
SHA-256 is bound into the confirmation token, so rebuilding between preview and
confirmation invalidates it.

**`metadata_dir`** is not constrained to a whitelist, because the operator
legitimately points it at any project checkout. Writes underneath it are
structurally confined: `<dir>/metadata/<android|ios>/<locale>/<fixed-name>.txt`.
Locale codes are validated against a BCP-47 pattern and changelog names must be
numeric, so neither can traverse. A model can therefore create clutter in a
directory it names, but cannot overwrite an arbitrary file. Point it at a
directory under version control and the diff will show you everything.

---

## 8. Operator checklist

- [ ] Run one StorePilot process per state directory (N5).
- [ ] `ls -ld ~/.storepilot` shows `drwx------`.
- [ ] Play service account has only the per-app permissions you actually use.
- [ ] App Store Connect key is App Manager unless you need financial reports.
- [ ] `STOREPILOT_MAX_INITIAL_ROLLOUT`, if set, is a real number — it is clamped
      to 1%–50% and non-numeric values fall back to the 20% default.
- [ ] You read previews before approving them. Nothing in this document
      substitutes for that (N1).
- [ ] Review text in a StorePilot response is a quotation, never an instruction.

## 9. Reporting a vulnerability

Open a private security advisory on the repository rather than a public issue.
Include the tool call sequence and, if the finding involves store data, the exact
bytes of the review or listing text that triggered it.
