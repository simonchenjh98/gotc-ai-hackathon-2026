# Release-blocker fixes and contestant acceptance

18 November 2026. The starting checkpoint is `3a96a18` on `codex/platform-rebuild`. All test accounts, uploads, traffic, provider responses, and databases were generated locally or in disposable CI. The limits below are startup defaults for a single gateway worker. The organizer decides the public origin, production resource budget and historical content policy.

## Contestant access and uploads

Each team has one contestant code, labeled **参赛码**. Claim redemption and organizer issuance use the same team account. One sign-in opened the profile, upload and submission workspace, private results, and the provisioned API-key management form. Reissuing the code kept the account, files and formal submission and invalidated the old code and sessions. Claim receipts identify applications, and SDK keys authorize model calls. A historical registration-only code retains its original narrow permissions. No current contestant operation requires that second code.

At the checkpoint, selecting a file on the local preview returned HTTP 200, but the browser rejected the local material URL and left the submission field blank. The repaired flow accepts an immutable upload owned by the current team on the configured event origin. External material links still require HTTPS. Real Chromium file-picker tests covered PPTX, replacement PDF, ZIP, draft reload, retry after refusal, and a formal v2 receipt. Judge downloads matched the original files byte for byte. The extracted-release test also posts binary files and submits their returned HTTPS material URLs through the same team session. [Upload observations and reproduction commands](2026-11-18-contestant-upload.md) · [Single-code HTTP journey](../../tests/backend/test_team_profile.py)

## Release gates

| Gate | Result and scope |
| --- | --- |
| Event-scale gateway capacity | The 69,266-call, 40-team synthetic replay rejected 207 calls (**0.299%**) with 15 running per team, 64 running globally, 16 queued per team and 64 queued globally. The checkpoint rejected 86.325% on this generated trace. In real separate service processes, **80/80** authenticated streams completed and all response bytes, token counts and charges matched. Login and draft-save p95 were **1.063×** and **1.882×** idle during the occupied/completion workload. [Method and raw evidence](2026-11-18-gateway-capacity.md) |
| Slow and large request bodies | A bounded upload stage authenticates and validates bodies before fair model admission. The original paused-body regression occupied a model slot. Its replacement holds zero while reading. Large request and reply materialization, the 256 MiB retained-spool budget, speech sessions and task polling have separate bounds. The HTTP provider pool defaults to 81 connections. Four 32 MiB decoded requests with 50,000,000-byte replies all succeeded with capture enabled. Peak macOS process RSS was 294.23 MiB. [Maximum-payload fixture](2026-11-18-large-upload-resource-check.json) |
| Disk-safe migration | Existing legacy content requires an explicit `migrate` or `discard` choice. Read-only preflight budgets the copy, WAL growth, journal allowance, historical snapshots where applicable and a 256 MiB reserve. An unnumbered 200 MiB fixture used **202.52 MiB** of additional logical space to migrate and **2.09 MiB** to discard. Peak filesystem allocation during migration was 211.35 MiB. Six interrupted processes resumed correctly. Backup tools reject an in-progress paired migration. Row removal leaves the main SQLite file allocated. [Space measurements, interruption tests and limits](2026-11-18-content-migration.json) · [Operator procedure](../../ops/deploy/README.md#legacy-gateway-content-and-disk-space) |
| Valid login bursts and lockout isolation | The password scheduler admits at most two concurrent expensive checks and bounds waiting outside normal API workers. IP and account-plus-IP controls limit incorrect proofs. Account pressure uses bounded cooling separately from ordinary credential checks. Clients retry only the explicit busy response with a bounded backoff. Five quiet waves accepted **200/200** valid logins, 40 per wave. Pooled authenticated API p95 was 2.658 ms idle and 1.654 ms during login. Other runs with competing local jobs had individual waves above 2×. [All timing samples and scheduler counters](2026-11-18-login-capacity.json) |
| Organizer TOTP storage | New event databases contain an enrollment fingerprint and replay counter. The operator supplies the actual seed through a separate private credential. The CLI stages enrollment. Only a successful named login through the running platform confirms the credential and retires legacy access. Missing or mismatched credentials fail safely. The offline checkpoint conversion and both backup tools account for older unreleased seed-bearing schemas. [Enrollment, confirmation and recovery](../organizer-accounts.md) |
| Edge and CI gates | The release rehearsal checks Nginx TLS/HTTP2, independent services, source and wheel allowlists, resource settings, and Git subprocess restrictions. Ruff and mypy cover additional HTTP, authentication and admission code. The deployment guide documents the shared Nginx connection budget, file descriptors and the independent service ceilings. [Checks and reproduction commands](2026-11-18-release-gates.md) |

## Reproduce locally

```sh
make check
make test
python tests/browser/smoke.py --smoke-only
python tests/performance/gateway_replay.py
python tests/performance/gateway_capacity.py
python tests/performance/login_capacity.py
```

Use the project virtual environment for Python commands and install the pinned Playwright test dependency for the browser journey. The linked evidence files give the exact benchmark arguments, seeds, interpreter versions, OS and sampling methods. Browser checks and process rehearsals use loopback or temporary Unix sockets.

## Rollout boundary

The replay uses generated timings and the 80-stream rehearsal uses small responses. The large-payload test covers a different synthetic workload. A passing combination of these tests does not establish Linux cgroup memory, page-cache or network occupancy on the shared target host. Confirm disk headroom before taking a release backup or selecting a migration mode. Secure deletion in the database does not erase previous snapshots. Production retention decisions, off-host backup and restore, host access policy, deployment identities, the domain and TLS setup remain with the organizer.
