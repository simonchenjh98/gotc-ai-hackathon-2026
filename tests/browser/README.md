# Chromium acceptance

Run from the repository root with Python 3.12 or later.

```sh
python3 -m venv .qa/browser
. .qa/browser/bin/activate
python -m pip install --only-binary=:all: --require-hashes -r backend/requirements.txt
python -m pip install -r tests/browser/requirements.txt
python -m playwright install chromium
python tests/browser/smoke.py --smoke-only
```

On a clean Linux CI runner, use `python -m playwright install --with-deps chromium`. To reuse an installed Chromium, pass its executable path with `--chromium`.

The check builds the fingerprinted frontend from the release allowlist, starts the real HTTP service on a random loopback port, creates a temporary database with synthetic teams and invitations, and walks through the public pages and each role. The organizer journey checks legacy portal sign-in before enrollment and named sign-in with a current TOTP after enrollment. Browser requests to other origins fail the test. The service and data disappear when the process exits.

The contestant journey uses a normal team code and Chromium's file picker. It uploads synthetic PPTX, PDF and ZIP files, verifies the team and project filenames, downloaded bytes and stored SHA-256, then saves a draft and reloads it. A judge sees the files after the team previews and confirms the formal submission. The same journey exercises a file above the 100 MiB ZIP limit, an invalid ZIP and a window closed mid-edit. It retries after reopening and checks the original form values.

For a repeatable cold-login measurement, run `python tests/browser/smoke.py --benchmark-only --runs 5`. Repeat with `--source /path/to/previous/checkout` using the same Chromium. Each sample starts in a fresh browser context with caching disabled, 40 ms of added latency, and 200,000 bytes/second of download bandwidth. The report records the first animation frame with a visible sign-in field, relative to navigation start, and decoded JavaScript resource sizes. The functional CI check has no performance threshold.
