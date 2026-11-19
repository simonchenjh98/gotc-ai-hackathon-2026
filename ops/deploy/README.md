# Build and deploy the isolated event services

## Build and rehearse an artifact

CI builds `release.tar.gz` and `release.json` after frontend, browser, backend, operations and hygiene checks. Its separate signing job also waits for the dependency audit, lint, types, secret scan and HTTPS edge tests. Only a protected `main` push or dispatch can produce the deployable `gotc-signed-release-<commit>` bundle. For a local rehearsal from a clean checkout, use `make wheels` followed by `python -m ops.deploy.deploy --package /tmp/gotc-release.tar.gz`. The builder checks wheel hashes, compiles content-addressed frontend assets, validates source paths, and includes the public allowlist. A copied wheelhouse works offline with `--wheelhouse DIRECTORY`. On a builder without Make, use `python -m pip download --platform any --only-binary=:all: --require-hashes -r backend/requirements.txt -d .runtime/wheels`.

To run the extracted artifact locally, use Python 3.11+ with venv and a private data directory.

```sh
python3 -m venv .venv
.venv/bin/python -Im pip --isolated install --no-index --only-binary=:all: --require-hashes --find-links wheels -r backend/requirements.txt
GOTC_DATA_DIR=/path/to/private-data GOTC_PUBLIC_ORIGIN=http://127.0.0.1:5274 .venv/bin/python -EsBu -m backend.gotc
```

To rehearse the production entry points, create a private socket directory with `RUNDIR=$(mktemp -d)`. Run `--service platform --uds "$RUNDIR/platform.sock"` and `--service gateway --uds "$RUNDIR/gateway.sock"` in separate processes using the same private synthetic event directory and an explicit HTTPS public origin. Probe both with `curl --unix-socket SOCKET http://localhost/readyz`. The CI package test makes authenticated requests against the extracted platform and verifies gateway readiness and access control.

GitHub Pages publishes a separate public-only landing build. Set the repository variable `GOTC_PUBLIC_ORIGIN` to the organizer-approved HTTPS origin before publishing. A preview can use `python -m ops.deploy.pages _site --public-origin https://event.example.invalid`. Integrated releases use their own origin.

## Provision the Linux host

The platform reads live organizer counters directly from `/run/gotc-gateway/http.sock`. For a local rehearsal with a different socket layout, set `GOTC_GATEWAY_SOCKET` on the platform process to the gateway's absolute socket path. The organizer dashboard will continue to show historical usage if the gateway is offline and mark its live counters unavailable.

The operator supplies the host settings, Python 3.11+ with venv/ensurepip, Git, Nginx with HTTP/2, curl, runuser, OpenSSH with `ssh-keygen -Y verify`, and systemd. On Debian/Ubuntu provision the matching `python3-venv` package. Provision these permissions before accepting a release.

- Make `/opt/gotc-hackathon`, its `.gotc-owned` marker, `releases/` and `backups/` root-owned. Root and the service/Nginx users need search permission on the release path. Restrict `backups/` to root. Only the mode-0700 `incoming/` directory is writable by the limited SSH account `gotc-deploy`. Provision the separate `gotc-build` system account with a non-login shell, no SSH access, no sudo rights, and a private home or `/nonexistent`. Keep the upload, build, application and Nginx identities distinct. The installer temporarily grants only the build UID write access to the new release’s virtual environment.
- Keep the existing private `data/` directory owned by `gotc-hackathon` with mode 0700. Both services use this UID to preserve existing SQLite and uploaded materials. Give the Nginx worker membership in the dedicated `gotc-proxy` group. Both socket directories share that group and have mode 0750.
- Create a dedicated Ed25519 key for this repository’s protected release workflow. Store its private key only in the GitHub environment secret `server-release-signing/GOTC_RELEASE_SIGNING_KEY`. Restrict that environment to protected `main` with required release reviewers and protect changes to the signing workflow. Provide the matching public key to the host operator using an independent channel.
- Install the public key in `/etc/gotc/ci-release-signers` as an OpenSSH allowed-signers entry of the form `gotc-ci-main namespaces="gotc-ci-release-v1" ssh-ed25519 <public-key-base64>`. The file and its containing directory must be root-owned, with write access restricted to root. The uploader can publish only signatures accepted by this policy.
- Write the selected public HTTPS origin alone in root-owned `/etc/gotc/public-origin`. Configure TLS/HTTP2 and the proxy's HTTP-context rate-limit zones using the [Nginx provisioning instructions](nginx.md). The installer edits only the managed GOTC locations in the matching HTTPS virtual host.
- The platform and gateway read optional `/etc/gotc/event.env`. Reserve `GOTC_PUBLIC_ORIGIN` for the origin file. Map event-specific environment overrides from the legacy service to this mode-0600 file or reviewed successor-unit drop-ins. If old `.service.d/*.conf` overrides or custom environment/credential directives in the legacy unit exist, the installer reports their aggregate digest. After checking each setting is accounted for, place that digest in root-owned `/etc/gotc/legacy-dropins.sha256`. Changing an old override requires a new review.
- Existing private `gateway.json`, `repositories.env`, `shared-login-codes.json`, `api-key-pepper`, `roadshow-claim-key`, `finals-access-code.txt` and optional `smtp.json` remain in the private data directory. For a file supplied through systemd, add e.g. `LoadCredential=gateway.json:/etc/gotc/credentials/gateway.json` to the appropriate successor units' root-owned drop-ins. Configure only credentials that exist for this event. Keep each file on the host.
- For the official `organizer` account, use its prepared private enrollment to provision the matching entry in the platform unit's TOTP map. Supply that unit's mounted credential view when staging the account. The first successful named portal login completes the cutover. Keep the enrollment and credential backups outside the release artifact. [Enrollment and recovery](../../docs/organizer-accounts.md#enroll-and-activate-an-organizer)
- Install the reviewed `ops/deploy/install.py` and `ops/deploy/nginx.py` under root-owned `/usr/local/lib/gotc-operator/ops/deploy/`. The privileged entry point `/usr/local/sbin/gotc-install` must launch these preprovisioned modules with an isolated interpreter and a fixed import path. A suitable root-owned launcher is shown below. Update these trusted files separately when the release allowlist changes. Grant `gotc-deploy` passwordless sudo only for this launcher. Keep the user's SSH identity and pinned host key on the operator's workstation.

```sh
#!/bin/sh
exec /usr/bin/python3 -IS -c 'import sys; sys.path.insert(0,"/usr/local/lib/gotc-operator"); from ops.deploy.install import main; main()' "$@"
```

## Promote and roll back

Download the protected job’s signed bundle for the reviewed commit. Keep `release.tar.gz.sig` beside `release.tar.gz` and obtain the archive SHA-256 from that run’s `release.statement`. This canonical statement binds the archive digest and source commit to the protected branch. The upload tool uses Paramiko on the operator's workstation. For an explicitly authorized deployment, invoke the following command with the selected host, commit and digest.

```sh
python -m ops.deploy.deploy --execute release.tar.gz --source-sha "$COMMIT" --sha256 "$ARCHIVE_SHA256" --host "$DEPLOY_HOST"
```

The client uploads the archive and detached signature as `gotc-deploy`. The trusted installer verifies the protected CI signature against its independently provisioned signer, checks the actual digest of a private archive snapshot, validates the exact application, wheel and static asset inventory, and prepares the virtual environment offline as `gotc-build`. Root freezes its ownership and permissions before activation. The upload account can also promote a previously signed release while its signer remains trusted, subject to the data compatibility limits below. It keeps each versioned release in place. Each service runs one worker. The systemd memory ceiling is 256 MiB for `gotc-platform` and 384 MiB for `gotc-gateway`. Their sockets are `/run/gotc-platform/http.sock` and `/run/gotc-gateway/http.sock`. Readiness and metrics are local to those sockets. The provisioned application credential stays in the private event data directory.

The gateway ceiling reflects a four-call synthetic rehearsal. Each call submitted 32 MiB of valid JSON and received a 50,000,000-byte JSON reply. Gateway process RSS peaked at 263.7 MiB with content capture off and 281.9 MiB with opt-in capture on. The 384 MiB limit leaves 102.1 MiB above the higher observed peak. A 320 MiB limit would leave 38.1 MiB. Allocation patterns can change with request shape, streaming behavior and concurrency. [Gateway memory rehearsal](../../tests/performance/gateway_memory.py)

Both services set `LimitNOFILE=1024`. The default gateway can have 272 admitted HTTP/WebSocket requests, 81 HTTP provider connections in its primary pool and eight provider speech sockets. This gives a planning subtotal of 361 socket descriptors before listeners, TLS/proxy pools, database handles, spools, logs and idle clients. Size descriptors and edge connections again if increasing admission settings. The 80-client synthetic stream rehearsal measured a gateway peak of 162 sampled descriptors and 80 settled accounting records, with zero reported SQLite lock failures. The four gateway worker threads handle short synchronous operations. Provider streams run on the event loop. Each production socket runs one Uvicorn process so admission, the bulk-memory lease and queues apply across the full service. Adding workers would multiply the limits. The platform allows eight ordinary worker threads and two password-check workers. Systemd limits threads plus processes to 48 for gateway and 80 for platform, including optional Git subprocesses. [Capacity results and measurement scope](../../docs/audits/2026-11-18-gateway-capacity.md)

Deploy capacity overrides in the root-owned service environment file `/etc/gotc/event.env` before startup. Each row gives the suffix after `GOTC_GATEWAY_`.

| Setting | Default | Scope |
| --- | ---: | --- |
| `TEAM_RUNNING` / `GLOBAL_RUNNING` | 15 / 64 | Ordinary model calls |
| `TEAM_QUEUE` / `GLOBAL_QUEUE` | 16 / 64 | Fair model queue, 30-second deadline |
| `TEAM_UPLOADS` / `GLOBAL_UPLOADS` | 8 / 96 | Concurrent authenticated body reads. At most two per team and 16 total may additionally wait, for up to two seconds. |
| `TEAM_TASKS` / `GLOBAL_TASKS` | 2 / 8 | Lightweight tasks and token counting |
| `TEAM_SPEECH` / `GLOBAL_SPEECH` | 2 / 8 | Speech WebSockets |
| `REQUEST_LIMIT` | Derived, 272 at defaults | Covers running, queued, upload, task and speech requests plus a reserve of 16 for fast requests. |
| `SPOOL_MIB` | 256 MiB | Process-wide limit for retained private request replay and capture files. Accepts 1–1024 MiB. |

Budget temporary space beyond the retained spool limit for one active large parse or reply, event/content databases, WAL and migration work. A request that would exceed the retained-byte cap receives a retryable 429 before forwarding.

Queue values may be zero. Other lane limits must be positive integers up to 1024, and each per-team limit must fit within its global counterpart. The derived request minimum accounts for every lane. Any explicit request limit must cover that sum and be at most 8192. Startup rejects invalid overrides. The model provider pool scales with model and task limits, one reconciler and eight organizer probes. Up to 16 idle keep-alive connections may remain in the pool. [Shared Nginx connection budget](nginx.md)

Before rollout, repeat the representative workloads on a production-like Linux host with these cgroup limits. Inspect each unit’s `memory.peak` and `memory.events` and check the host’s available memory and pressure while neighboring workloads are active. The two independent ceilings total 640 MiB. They reserve no RAM. Confirm the remaining capacity supports the kernel, Nginx and the other services under concurrent load.

The installer checks neighboring services, backs up Nginx, the two unit files, SQLite and its bound roadshow key, then switches the services and public asset pointer. It checks both Unix sockets, the external directory endpoint and neighboring units. A failed activation restores the saved units, prior activation state and public asset pointer. Concurrent Nginx edits are preserved. An operator performing a manual rollback must restore the matching service units, release pointer and managed Nginx configuration together. Keep the old virtual environment throughout the rollback window and review the data-compatibility restrictions below.

## Team recycle migration and rollback floor

The release includes `backend/gotc/teams/team_lifecycle.py` and the existing admin assets through
the normal allowlist. Initialization adds two lifecycle/history tables and an index;
it does not rebuild existing tables or backfill identities. Use the installer's
SQLite backup and normal health checks before activation. Keep the data directory
outside the release tree. Initial production smoke should be read-only; this feature
does not authorize recycling any real team as a deployment test.

Recycling preserves the original team ID/number, submissions, scores, votes, frozen
rosters, repository records and usage, but releases current memberships and revokes
team credentials/sessions. A recycled name is available for new approval. Restore
is atomic and rejects current name or verified-member conflicts; it cannot replace
another team's members. Successful restore retains the previous eligibility status
and leaves old access inactive. Operators must separately reissue submission codes
and enable API access/create new keys when access is needed. Team profile management
uses the contestant session; existing valid registration codes remain limited to their original profile-edit capability, and recycled codes stay revoked.
Review and explicitly republish affected stage results.

Unified team access adds the idempotent `team_profile_visibility` metadata table.
New own-team profile rows are private; historical public registrations keep their
existing directory visibility. Preserve this filter on subsequent saves, approvals
and restoration. Legacy registration hashes remain stored for data preservation,
with existing-only open/save compatibility; create and administrator rotate still
return 410 and imports issue no code. A compatible rollback must retain the
visibility filter and profile/identity boundaries: an older binary can expose
new private profiles even though valid legacy codes are intentionally supported.
Do not reset contestant accounts, codes, sessions or the database for this update.

**After the first real recycle, rollback must retain lifecycle-aware code.** An older
binary cannot interpret tombstones or archived memberships and may permit ordinary
editing/reactivation to bypass restore checks. The installer's automatic health
rollback is not proof of semantic safety after feature use. Prefer a forward fix or
a reviewed compatible rollback; never overwrite later event activity with an old
whole-database backup. Before any lifecycle mutation, unused additive tables alone
do not introduce this post-use incompatibility. No destructive rollback or real
data deletion is part of the synthetic release verification.

Gateway revocation takes effect at the existing final admission recheck after a
queue wait. Requests already admitted may complete and remain billed/auditable;
there is no network-instant cancellation or erasure of incurred usage.

## Legacy gateway content and disk space

Obtain the organizer's retention decision before activation. Stop both old processes, jobs and database readers for the upgrade window. If the legacy `api_logs` table contains rows, configure one of the following values in the private `/etc/gotc/event.env` shared by both services.

| Setting | Effect on legacy content |
| --- | --- |
| `GOTC_LEGACY_GATEWAY_CONTENT=migrate` | Copy the newest rows that fit the configured age and byte limits into the private sidecar. Remove processed source rows, including those excluded by retention. |
| `GOTC_LEGACY_GATEWAY_CONTENT=discard` | Remove the legacy bodies without copying them. Billing, token counts, usage IDs and request metadata stay in the event ledger. |

Missing or invalid choices stop startup before provisioning private files. Read-only inspection can refresh the transient SQLite shared-memory index for a crash-left WAL. Insufficient space reports free, required, copy, journal, historical snapshot, existing WAL checkpoint growth and reserve bytes. Check the free space on the filesystem containing `GOTC_DATA_DIR`. The installer also writes its own pre-activation backup. The installer checks the backup filesystem for one event-database extent plus a 256 MiB reserve before creating its bundle. Protect any pre-upgrade copy containing conversations according to the organizer's decision.

The startup calculation reserves 256 MiB of free space. Copy capacity is 110% of the eligible bytes still to copy plus one sidecar page and 256 bytes per row (4,352 bytes for 4 KiB pages). The journal allowance includes twice the larger of 4 MiB or the largest individual stored record, 2,048 pages at the larger database page size and 4 MiB for other SQLite files. If committed WAL pages extend either database beyond its main file, the calculation also reserves that growth before truncating the WAL. Earlier unnumbered domain migrations that require historical snapshots add an event-database extent plus 2 MiB per snapshot. Discard has a zero copy budget. The 200 MiB rehearsal used 4 KiB pages and a 20 MiB journal allowance. The code checks free space again between batches. [Measured space and restart evidence](../../docs/audits/2026-11-18-content-migration.json)

The migrator commits up to 128 rows or 4 MiB at a time. A larger individual row gets its own batch. It syncs the sidecar, verifies copied bytes and request identities, then securely deletes that batch from the event database. Both WALs undergo truncating checkpoints. A blocked checkpoint stops further batches. After an interruption, keep the same event database, private sidecar and SQLite auxiliary files together, stop any remaining readers, and restart with the original choice. The saved age/quota boundary and sidecar identity allow replay; entries that expire during downtime also age out. Remove the setting from both services after the upgrade succeeds so any later rollback-generated rows require a fresh decision.

The old release reads bodies from the event database. **It immediately loses access to each deleted batch**, even during an interrupted migration. A binary rollback only restores the old code. A historical backup can restore rows as of its snapshot and needs a separate recovery plan to preserve later event activity. The installer and the portable backup command refuse an intermediate migration snapshot because routine event backups omit the private sidecar. Complete or recover the paired migration before creating a new backup. Routine backups after completion exclude the sidecar and its journals.

Secure deletion overwrites deleted ordinary table cells through SQLite. A truncating checkpoint requests that committed pages reach the main file and releases WAL space. Filesystem snapshots, prior backups, copy-on-write history, earlier journals and device-level remnants can still hold earlier bytes. Deleting rows makes space reusable inside `event.sqlite3`; its file generally keeps its previous size. Schedule an offline `VACUUM` separately if reclaiming filesystem space is necessary. Budget up to twice the original database size in additional free space, retain the host reserve and verify the backup first. [SQLite secure deletion](https://sqlite.org/pragma.html#pragma_secure_delete) · [SQLite VACUUM space requirements](https://sqlite.org/lang_vacuum.html#how_vacuum_works)

## Contestant material uploads

The team workbench accepts PPT/PPTX/PDF (50 MiB), supporting ZIP archives
(100 MiB), and standalone UTF-8 HTML (10 MiB). The dedicated
`/gotc/api/team/uploads/` nginx location streams requests to the application;
the generic API body limit stays unchanged. The service authenticates the team
and rechecks the submission window, session and 500 MiB team quota before
committing an upload. Archives are never extracted. HTML uses a response-level
opaque-origin CSP sandbox, and all other material files are attachments.

Completed files live in `data/material-uploads/`; `material_uploads` database
rows retain their immutable IDs, hashes and filenames. Keep this directory with
the database across releases. `ops/backup_event.py backup|restore` copies
the completed files referenced by the database snapshot, verifies their size
and SHA-256, and refuses to replace a different existing destination file.
The installer takes an in-place SQLite snapshot and copies the roadshow claim
key before switching releases. Its rollback continues to use the existing live
data directory. The portable backup command covers the database, the roadshow
claim key, and the completed material files referenced by that database.

For recovery on a clean host, retain a consistent backup of event records and referenced materials. Keep the required credential set and launch configuration separately under restricted access, including the original API-key pepper, roadshow key, configured provider and login files, and any referenced systemd credentials. An existing API key with a missing pepper stops initialization. Restore the matched credentials, data and file permissions before starting the two services.

Exclude `gateway-content.sqlite3`, `gateway-content.sqlite3-wal` and `gateway-content.sqlite3-shm` from routine event snapshots and filesystem backups. The content store has its own expiry schedule. An exceptional protected copy requires separate authorization, access controls and a defined expiry.

The uploaded file URL is preparation only: it becomes official when the team
confirms a formal submission and receives its versioned receipt. Existing
formal versions keep their original URLs when a replacement file is uploaded.

## Optional private source backup worker

The sealed Gitee source worker additionally requires the system Git executable (production baseline: Git 2.43.0) and Python 3.12; no pip dependency is added. Configure `GOTC_REPO_GITEE_ENABLED=1`, the existing private Gitee organization/token settings, and `GOTC_REPO_GITEE_SEAL_ENABLED=1` for sealed previews. Set `GOTC_REPO_BACKUP_ENABLED=1` in the private service environment to start one worker from `main()` after HTTP server initialization. The GOTC event deployment uses GOTC_REPO_BACKUP_ENABLED=0 with Gitee sealing enabled: new versions have no archive job and existing private downloads remain available. Any other value than 1 leaves the worker stopped; constructing EventService in tests starts no threads. The Gitee token's mandatory user_info scope supports validating its login via `/user`; no additional scope is requested here. Optional: `GOTC_REPO_AUTO_APPROVE=1` creates a team's repository as soon as it applies (no admin approval click), and `GOTC_REPO_GITEE_JOIN_URL=https://gitee.com/...` shows participants a join-organization button.

Allow service writes beneath `GOTC_DATA_DIR`, including `repository-backups`, and outbound HTTPS to official Gitee. Never put credentials in command arguments, remotes, the release tree or logs. The worker uses a temporary private askpass helper and a restricted child environment; it resets system/global Git config, helpers, redirects and hooks, with `pack.threads=1` and `pack.windowMemory=32m` within the 256 MiB platform service limit. Source backups remain private and must not be exposed through Nginx aliases or copied into static releases.

Limits: one worker, polling every 5 s, three attempts with 60/300 s delays, 180 s per task Git/creation wall time, 268435456 task bytes, 2147483648 total backup bytes, 134217728 source bytes and 20000 tracked files. Account for temporary files and retained artifacts in disk capacity and private host backups. Failure keeps the original submission and SHA; restart recovers interrupted jobs. Shutdown stops the worker and its active Git process group. LFS/submodules produce partial backups; PPT/video and runtime dependencies are outside this archive. See [repository provisioning](../../docs/repository-provisioning.md) for private API and operator boundaries.
