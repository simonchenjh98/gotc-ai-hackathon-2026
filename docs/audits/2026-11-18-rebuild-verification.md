# GOTC rebuild verification

18 November 2026. The reconstruction uses local commit `8f009f9` as its behavior baseline. That commit reconciles the organizer’s Git history with the application files from the running release. Runtime changes are in `82292d9`. The production host was used for read-only identification and source retrieval. All acceptance runs used local synthetic data.

## Measured performance

| Measurement | Deployed source | Rebuilt source | Change |
| --- | ---: | ---: | ---: |
| Contestant login, first visible input | 3,143.5 ms | 506.9 ms | 83.9% less time |
| JavaScript decoded on that navigation | 514,575 bytes in 10 scripts | 37,596 bytes in 5 scripts | 92.7% fewer bytes |
| Authenticated standings handler, median | 9.290 ms | 1.101 ms | 88.1% less time |
| Authenticated standings handler, p95 | 9.566 ms | 1.208 ms | 87.4% less time |

The login measurement used Chromium 151, five fresh cache-disabled browser contexts per version, 200,000 bytes/s download, 100,000 bytes/s upload and 40 ms latency. The clock ran from navigation start to the first animation frame with a visible login field. The baseline used the locally served deployment source with normalized text line endings. The 516,816 bytes in the [initial audit](2026-11-18-platform.md) are the original production response bodies. The browser trials did not measure public internet congestion or production load.

The standings measurement used Python 3.12 and a fresh database per run, with 128 teams, 1,024 submitted scores and 3,072 votes. Each version ran five times in alternating order. Each run performed ten warm-ups and 50 authenticated reads. The table gives the median of the five run medians and the median of the five p95s. SQLite’s query plan used the new team-leading vote and submitted-score indexes.

## Functional acceptance

- The documented `make test PYTHON=python3.12` command passed in the delivery checkout. It ran 586 frontend tests, 779 backend tests and 19 operations tests, for **1,384 passing automated tests**. The backend suite also passed on macOS Python 3.14. The Python 3.11/3.12 CI matrix, browser job and packaging checks are configured in the repository.
- Nine Chromium journey groups exercised mobile public navigation, a claim and receipt, professional/exhibition/roadshow judging, finals access and seat selection, audience voting, team and organizer workspaces, delegated access, and a delayed module completing after navigation. The test checked JavaScript errors, failed HTTP responses and unexpected outbound requests. [Browser acceptance instructions](../../tests/browser/README.md)
- Starting the deployed service with temporary data, creating a team and judge, and reopening the same database in the rebuilt service preserved the logical contents of 65 tables. The synthetic private files matched, the existing organizer session remained valid, and the authenticated standings reflected the saved team.
- A release was built twice from a clean checkout. The two 116-file archives were byte-identical. Their SHA-256 was `e7151d7000ac066234392f5990e1d55b5e7e393a59ff3dce5d6b3def1aaa38aa`. The release tests exercised extracted-package startup, role checks, team submission, judging and installer recovery. The separately staged Pages export passed public link and asset checks.

## Maintainability and release status

The original repository root contained 21 HTML, JavaScript and CSS files. The rebuilt root has zero application source files. Browser features live under `frontend/static/`. HTTP, persistence, teams, submissions, judging and gateway responsibilities have separate Python modules. CI checks the directory boundaries, including imports from runtime code into development tooling. [Architecture guide](../architecture.md)

Independent review found stale sign-in responses could change ownership across tabs. The ordinary, roadshow and delegated login flows now confirm the session before publishing an identity change. Review also led to a common data-directory check across launch paths and per-service checks in the installer. Tests cover these fixes.

The deliverable is a local review branch. A production rollout, a live upstream-provider load test and a hosted CI run fall outside this acceptance record.
