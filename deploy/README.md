# Isolated cloud deployment

`deploy.py` creates a local release package by default. It changes the server only
when explicitly run with `--execute`:

```powershell
python deploy/deploy.py
python deploy/deploy.py --execute
```

Commit this project's source first. The script records the Git commit SHA, content
digest, release archive SHA-256, URL and health result in `.runtime/deployment.json`.
Without a project-owned Git repository it records the source content SHA-256.
The release includes public HTML/CSS/JS (including `platform-admin.js`), approved
assets/downloads, backend Python source and `scripts/backup_event.py`. The backup
script is an operator CLI, not an HTTP download. Data, databases, private access
files, tests and QA are excluded.

The deploy tool needs local Paramiko and reuses the existing verified SSH identity
at `~/.ssh/realtalk_ecs` with `~/.ssh/known_hosts` and strict host-key checking.
It never reads the key into logs, shell arguments or the repository.

The new service uses `/opt/gotc-hackathon`, system user/service `gotc-hackathon`,
loopback port 5274, and `https://8.141.114.201/gotc/`. Python 3.12 standard library
is sufficient on the server. Nginx preserves `/gotc` when proxying. Its managed
location allows a 2 MiB JSON body only for `/gotc/api/admin/data/import/preview`
so a legal 256 KiB CSV can pass after JSON escaping; every other `/gotc/` request
keeps the 32 KiB proxy limit, and the application still enforces its own
per-endpoint limits. The 27 September event routes and databases are independent
and untouched.

Before activation the installer backs up Nginx and any existing GOTC SQLite
database. It verifies the new service locally, tests and reloads Nginx, checks
the public board API, and confirms the original services are active. Failure
restores this service's prior current release/unit and the Nginx edit; data is
preserved. A concurrent Nginx edit is never overwritten automatically.

The initial admin access file is transferred through SFTP to
`.runtime/admin-access.txt`. It is never printed and must never be committed or
shared publicly. Keep `.runtime/` ignored by Git.

## Team recycle migration and rollback floor

The release includes `server/team_lifecycle.py` and the existing admin assets through
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
the database across releases. `scripts/backup_event.py backup|restore` copies
the completed files referenced by the database snapshot, verifies their size
and SHA-256, and refuses to replace a different existing destination file.
Do not restore a database alone into a new data directory. The installer's
in-place rollback snapshots continue to use the existing live data directory;
use the backup script for a portable database-and-files backup.

The uploaded file URL is preparation only: it becomes official when the team
confirms a formal submission and receives its versioned receipt. Existing
formal versions keep their original URLs when a replacement file is uploaded.

## Optional private source backup worker

The sealed Gitee source worker additionally requires the system Git executable (production baseline: Git 2.43.0) and Python 3.12; no pip dependency is added. Configure `GOTC_REPO_GITEE_ENABLED=1`, the existing private Gitee organization/token settings, and `GOTC_REPO_GITEE_SEAL_ENABLED=1` for sealed previews. Set `GOTC_REPO_BACKUP_ENABLED=1` in the private service environment to start one worker from `main()` after HTTP server initialization. The GOTC event deployment uses GOTC_REPO_BACKUP_ENABLED=0 with Gitee sealing enabled: new versions have no archive job and existing private downloads remain available. Any other value than 1 leaves the worker stopped; constructing EventService in tests starts no threads. The Gitee token's mandatory user_info scope supports validating its login via `/user`; no additional scope is requested here. Optional: `GOTC_REPO_AUTO_APPROVE=1` creates a team's repository as soon as it applies (no admin approval click), and `GOTC_REPO_GITEE_JOIN_URL=https://gitee.com/...` shows participants a join-organization button.

Allow service writes beneath `GOTC_DATA_DIR`, including `repository-backups`, and outbound HTTPS to official Gitee. Never put credentials in command arguments, remotes, the release tree or logs. The worker uses a temporary private askpass helper and a restricted child environment; it resets system/global Git config, helpers, redirects and hooks, with `pack.threads=1` and `pack.windowMemory=32m` for the existing 512 MiB service budget. Source backups remain private and must not be exposed through Nginx aliases or copied into static releases.

Limits: one worker, polling every 5 s, three attempts with 60/300 s delays, 180 s per task Git/creation wall time, 268435456 task bytes, 2147483648 total backup bytes, 134217728 source bytes and 20000 tracked files. Account for temporary files and retained artifacts in disk capacity and private host backups. Failure keeps the original submission and SHA; restart recovers interrupted jobs. Shutdown stops the worker and its active Git process group. LFS/submodules produce partial backups; PPT/video and runtime dependencies are outside this archive. See [repository provisioning](../docs/repository-provisioning.md) for private API and operator boundaries.
