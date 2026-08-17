# Contributing

## Setup

```bash
git clone https://github.com/sonlenef/storepilot-mcp
cd storepilot-mcp
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest        # 503 tests, no network, no credentials
.venv/bin/ruff check .
```

Tests must pass with no credentials configured and no network access. A
session-scoped fixture hashes `~/.storepilot` before the run and fails if
anything there changed, so a test can never touch your real guard key, audit log
or cached reports.

## The most useful contribution

Run StorePilot against a real store account and report what breaks. Everything in
[docs/ROADMAP.md](docs/ROADMAP.md) under "live-credential verification backlog" is
unverified, and two items carry headline claims: whether the Play Reporting API
returns rates as percentages or fractions, and what the real earnings CSV columns
are called. Both need an account bigger than the test one.

Live tests are marked `@pytest.mark.live` and excluded by default via `addopts`.
Run them deliberately with `pytest -m live`. No live test writes to a store.

## Ground rules

**Never make a missing measurement look like a good one.** An app with no vitals
data is not a healthy app; a month whose report has not been published is not a
month with zero installs. Every tool says which it is. This is the single rule
that most shapes the code, and a patch that trades it for a tidier table will be
rejected.

**Currencies are never summed.** There is no exchange rate here, and inventing
one produces a number that looks right and is not.

**Writes go through `core/guards.py`.** A new write tool takes
`confirm: bool = False, confirmation_token: str | None = None`, returns a preview
on the first call and mutates only on the second. Do not add a bypass, and do not
weaken the token to a plain content hash — a hash is computable by the model
itself, which would let it self-confirm without ever showing a human the preview.

**Errors carry a remedy.** Raise from `core/errors.py` with a `remedy` that names
the exact console, page and setting. A bare "permission denied" costs the user an
afternoon; the live runs so far have shown the remedy text is wrong more often
than the code is.

**Parameter descriptions live in the schema.** Use
`Annotated[T, Field(description=...)]`. An `Args:` docstring section is not read
by the SDK, so a tool can look documented in source while handing the model a
bare parameter name.

## Adding a store field

Parsing belongs in `core/csv_reports.py` (pure, offline-testable) and network I/O
in the adapter. Play reports are UTF-16 and their columns change without notice,
so resolve columns by name and fail loudly with the available headers rather than
returning a plausible wrong number.
