# Platform and gateway acceptance, 2026-11-18

The comparison used an archive of `d0b6042cb57fdf031ff3bead0da9db258bacf01f` and the candidate working tree on the same macOS 27.0.1 ARM64 host, with Python 3.12.12. The candidate ran Uvicorn 0.54.0, Starlette 1.10.0 and HTTPX 0.28.1. Every fixture created a temporary private event directory with synthetic accounts. All traffic stayed on local Unix sockets or loopback. Measurements cover this host and fixture size. Production TLS, Nginx, Linux systemd limits and real provider behavior need separate rollout verification.

## Login and draft latency

| Operation | Revision | Idle p50 / p95 (ms) | Occupied p50 / p95 (ms) | Occupied / idle p95 |
| --- | --- | ---: | ---: | ---: |
| Contestant login | `d0b6042` | 21.600 / 22.581 | 21.781 / 23.829 | 1.055× |
| Contestant login | ASGI | 20.218 / 21.057 | 20.689 / 21.679 | **1.030×** |
| Draft submission | `d0b6042` | 2.667 / 3.031 | 2.603 / 3.061 | 1.010× |
| Draft submission | ASGI | 1.238 / 1.407 | 1.303 / 1.585 | **1.127×** |

Both ASGI operations met the 2× p95 target in this run. Each sample paired a valid contestant login with an authenticated draft save, synchronized at the client. The test checked the returned role and each successive submission revision. Four disposable instances each supplied six measured pairs per condition after one discarded pair. The test alternated which condition ran first. The p95 is the nearest-rank percentile of the 24 samples.

In the occupied condition, one synthetic team sent two ordinary model requests to a local provider. The test waited until the provider received both calls, held them while timing the platform operations, then required both provider responses to complete successfully. The team cap was two on both revisions. On the candidate the fixture also set the global running limit to two. At the pre-fix checkpoint, the runtime defaults were two per team and four globally. The [gateway capacity rehearsal](2026-11-18-gateway-capacity.md) records the revised defaults and the 80-call results. Each occupied window reported two running and zero queued through the gateway's metrics endpoint. This models full admitted capacity under the specified fixture profile. [Reference samples](d0b6042-isolation.json) · [ASGI samples and observed counters](asgi-isolation.json)

## HTTP and browser behavior

The candidate launched separate platform and gateway processes through the production `backend.gotc` module. Each bound a different private Unix socket and passed its own readiness check. Sending a platform API path to the gateway socket and an SDK path to the platform socket returned 404. The 20 normalized HTTP observations matched `d0b6042`. These cover secure session cookie attributes, authentication and identity requirements, origin checks, CORS preflights, versioned drafts, stale edits, organizer CSV, ZIP and sandboxed HTML materials, GET/HEAD, and conditional HTML retrieval. The recorded material bytes were also verified round-trip. [Reference contract](d0b6042-http.json) · [ASGI contract](asgi-http.json)

| Real Chromium result | `d0b6042` | Packaged ASGI candidate |
| --- | ---: | ---: |
| First visible contestant sign-in input, median | 505.3 ms | 506.5 ms |
| Cold, decoded JavaScript bytes | 37,596 | 40,267 |
| Eager scripts | 5 | 5 |
| Passed browser journey groups | 9 | 10 |

Chromium 151.0.7922.34 used five independent cache-disabled contexts per revision with 40 ms of added latency, 200,000 bytes/s download and 100,000 bytes/s upload. The sign-in measurement starts at navigation start and ends on the first animation frame with a visible sign-in field. The browser used the candidate's real combined development server on loopback to navigate the public pages and role interfaces. The fixture staged the release allowlist and the same hashed JS/CSS as deployment. The sign-in page deferred the route manifest. The first route requiring it loaded one SHA-384-pinned manifest, reused across subsequent navigation. Browser checks cover mobile navigation, the directory, application and receipt, judge and finals flows, a persisted audience vote, team workbench, organizer delegation, named/TOTP and pre-enrollment organizer login, and delayed scripts after a route change. [Reference browser results](d0b6042-browser.json) · [ASGI browser results](asgi-browser.json)

## Reproduce

From the candidate checkout, install its locked runtime and the optional browser test dependency into an isolated environment. Use the same Python and Chromium executables for both revisions. `mktemp` creates a private baseline directory.

```sh
python3.12 -m venv .qa/acceptance
. .qa/acceptance/bin/activate
python -m pip install --only-binary=:all: --require-hashes -r backend/requirements.txt
python -m pip install -r tests/browser/requirements.txt
python -m playwright install chromium
baseline_dir=$(mktemp -d)
git archive d0b6042 | tar -x -C "$baseline_dir"
python -B tests/performance/contract.py --layout baseline --source "$baseline_dir" --compare docs/audits/d0b6042-http.json
python -B tests/performance/contract.py --layout uds --compare docs/audits/d0b6042-http.json
python -B tests/performance/isolation.py --layout baseline --source "$baseline_dir"
python -B tests/performance/isolation.py --layout uds
python -B "$baseline_dir/tests/browser/smoke.py" --source "$baseline_dir" --runs 5
python -B tests/browser/smoke.py --runs 5
```

For the exact browser build used here, pass `--chromium /path/to/chrome-headless-shell` to both browser commands. The HTTP scripts print only normalized contract data or timing samples. The benchmark never accepts a remote target. The test processes stop and delete their synthetic state on completion.
