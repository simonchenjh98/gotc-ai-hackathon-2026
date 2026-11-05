# Gitee Sealed Submissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lock Gitee versions using lightweight protected refs, confirm before the deadline without remote I/O, and back up the selected source asynchronously.

**Architecture:** A dedicated RepositorySeals module owns protected ref creation and local proof. A RepositoryBackups module consumes immutable formal-version links and produces private ZIP/bundle/manifest artifacts without holding a submission request. Frontend and existing receipt/history views display exact-version links and separate backup state.

**Tech Stack:** Python 3.12+ standard library, Git CLI only in the backup worker, SQLite, native JavaScript, unittest, Node test runner.

## Global Constraints

- Worktree: /Users/xinlu/Documents/ChatGPT/GOTC/gotc-ai-hackathon-2026/.runtime/worktrees/repository-provisioning; branch codex/gitee-sealed-submissions; starting source fa8ee08b272840fa589fc9ea1dcb8a2845012e48.
- Approved design: docs/superpowers/specs/2026-11-05-gitee-sealed-submissions-design.md. Keep its exact defaults and limits.
- Ordinary-account enforcement is an explicitly accepted assumption, not a live pass. Admin contents API can append despite deny-all Git rules. Never treat a new branch head as the confirmed SHA.
- No normal participant gets admin; no token/SSH key/database/runtime artifacts in commits, tests, screenshots, reports or PRs. Do not inspect .runtime credentials or call live providers from implementation/review agents.
- GitHub, legacy manual submissions, scoring/voting, GitCode, immutable history and exact receipt replay stay compatible. No public publication feature or PPT/video archiver in this change.
- Gitee opt-in: GOTC_REPO_GITEE_SEAL_ENABLED=1. Worker opt-in: GOTC_REPO_BACKUP_ENABLED=1. No source download on preview or confirm.
- Worker limits: one worker, poll 5 s, 3 attempts, retry delays [60,300] s, Git wall clock 180 s, single-task bytes 268435456, total archive bytes 2147483648, source bytes 134217728, files 20000.

---

### Task 1: Protected Gitee refs and local confirmation

**Files:** Create server/repository_seals.py and server/test_repository_seals.py; modify server/repositories.py, server/app.py, server/submission_records.py only for integration of seals/receivedAt. Update docs/repository-provisioning.md with the protocol.

**Interfaces:**
- Produces `Repositories.seals` as `RepositorySeals(self)`; `RepositorySeals.initialize(db)` executes only idempotent additional tables.
- `RepositorySeals.ensure(provider, binding: dict, sha: str) -> dict` returns `{id, ref, sha, sealedAt, viewUrl}` after verified protected refs; all upstream I/O outside DB writers. Uses persistent random namespace and same-SHA reuse/reconciliation; match repo numeric ID and binding revision. viewUrl is fixed `https://gitee.com/{organization}/{slug}/commit/{sha}`.
- `RepositorySeals.for_preview(db, preview_id: str) -> dict|None`, `RepositorySeals.verify_proof(db, binding, preview, proof)` enforce local ownership and SHA equality; optional preview validation reference time is trusted receivedAt.
- `RepositorySeals.record_submission(db, team_id: str, version: int, request_id: str, proof: dict) -> dict|None` inserts immutable association after submission_history insertion in the same transaction; returns stable proof `{id,ref,sha,sealedAt,viewUrl,receivedAt}`.
- `RepositorySeals.for_version(db, team_id: str, version: int|None) -> dict|None` returns that stable proof. This is Task 2's job input and Task 3's `repositorySeal` property.
- New preview adds `repositorySeal` only when enabled and fully sealed. Formal receipt and history detail add `repositorySeal`; existing keys and immutable replay unchanged. Latest team response adds `repositorySeal` (null for old versions).
- HTTP/dispatch accepts internal `server_received_at=None`, captures it after full JSON validation and before dispatch. POST body cannot set it. Receipt uses `receivedAt` when sealed; `savedAt`/submittedAt remain actual database save time. `save_submission(..., accepted_at=None)` uses trusted accepted_at only for the deadline comparison, current config permissions otherwise remain effective.

- [ ] **Step 1: Add failing HTTP regressions.** Use the existing real local HTTP fixture, override the Gitee request adapter only. Cover a fully sealed preview, confirmation with every upstream call failing, replay, selection unchanged after main advances, malformed/mismatched protection responses, same-name wrong-SHA rejection, lost response recovery, changed binding/session, expired preview, closed window, and trusted request arrival before deadline while a DB lock delays saving. Code skeleton:

```python
def test_sealed_confirmation_does_not_call_gitee(self):
    preview=self.sealed_preview()
    self.remote.fail_all=True
    response=self.formal_submit(preview)
    self.assertEqual(response[0],200)
    self.assertEqual(response[1]['receipt']['materials']['codeVersion'],preview['sha'])
    self.assertEqual(response[1]['receipt']['repositorySeal']['sha'],preview['sha'])
```

Implement the named helpers in the test fixture; no real network. Test malformed envelopes with a body receivedAt field return 400.

- [ ] **Step 2: Run red tests.** `python3 -m unittest discover -s server -p test_repository_seals.py -q`; expect failures for missing seal proof/local confirmation. Use localhost-enabled execution.

- [ ] **Step 3: Implement the module and wire it.** Persist a cryptographically random namespace before remote mutations. `PUT .../branches/setting/new` with wildcard and pusher/merger admin protects branch creation; create a per-SHA ref using POST branches with refs=full SHA; add exact none/none rule, verify response id/project/wildcard/pushers/mergers/mode and GET branch protected/sha. Retry only the same persisted operation/name, never overwrite wrong SHA. For already-created rules, the corresponding setting update may reconcile only that locally-owned random name. Cache fully sealed refs but read/verify on a new preview. No remote work inside SQLite writers. Under flag-off/GitHub retain old head check. A sealed confirm validates only local preview+seal+binding+session+window and supplies receivedAt proof to the writer. Material override restrictions remain.

- [ ] **Step 4: Attach immutable proof and received time.** Store preview-to-seal and version-to-seal associations in new tables, avoiding edits to existing immutable history/receipt rows. Capture trusted request arrival; verify not user-provided. Final formal save revalidates local proof atomically; old APIs/callers without receivedAt retain existing behavior.

- [ ] **Step 5: Run green and compatibility.** Run the new suite plus `python3 -m unittest discover -s server -p test_repositories.py -q` and `python3 -m unittest discover -s server -p test_submissions.py -q`. Include exact commands/counts in report, commit an explicit allowlist, and self-review.

### Task 2: Bounded asynchronous private source backups

**Files:** Create server/repository_backups.py and server/test_repository_backups.py. Modify server/app.py and server/submission_records.py for queue initialization/enqueue and worker lifecycle/private downloads. Update docs/repository-provisioning.md and deploy/README.md for Git and opt-in flags.

**Interfaces:**
- Consumes Task 1 `RepositorySeals.for_version(db,team_id,version)` and stable proof fields above. Uses bound repository numeric ID/organization/slug and selected SHA.
- Produces `EventService.repository_backups=RepositoryBackups(service)` with `initialize(db)`, `enqueue(db,team_id,version,request_id)`, `for_version(db,team_id,version)`, `run_once()`, `start()`, `stop()`.
- `RepositoryBackups.for_version` public metadata shape `{status,sha,attempts,error,sourceZipSha256,bundleSha256,missingAssets,downloadUrl,bundleUrl}` or null. URLs are same-origin API paths and never expose local paths. ready/partial requires verified files; queued/failed has no download URL.
- Latest team response adds `repositoryBackup`; submission history detail adds `repositoryBackup`. New formal submission enqueues once inside its existing atomic transaction after seal association. Old/unsealed submissions have no task.
- Private GET `/api/submission/backups/{team_id}/{version}/source.zip` and `/repo.bundle` resolve only exact managed artifact IDs and authorize admin or owning team. Anonymous/other-team denied. No public archive endpoint. Existing actor resolution applies, no token in download links.
- `ArchiveDownload` value object is returned to EventHandler for streaming with Content-Disposition, nosniff and fixed MIME; do not turn bytes into CSV or read entire large file into memory. Private artifact root is beneath service.data_dir only.
- Worker starts only in main(), opt-in 1, after HTTP initialization; constructing test services does not start threads. Shutdown stops worker/child Git. SQLite claim/finish transactions exclude remote work.

- [ ] **Step 1: Add failing tests.** Use a real temporary Git repository with deterministic commits A then B, mocked/injected fetch seam (no live provider), and real bundle/ZIP generation. Include these cases:

```python
def test_backup_preserves_selected_sha_when_source_head_advanced(self):
    self.enqueue_selected(self.sha_a)
    self.advance_source_to(self.sha_b)
    self.backups.run_once()
    record=self.backups.for_version(self.db,self.team_id,1)
    self.assertEqual(record['sha'],self.sha_a)
    self.assertEqual(self.read_zip_file(record,'version.txt'),'A')
```

Implement helpers concretely in the fixture. Also test export-ignore files included, standalone bundle verify/restore, LFS/submodule partial, unsafe path rejection, job dedup/restart recovery, bounded retry/backoff, disk/source/file-count/timeout limits, no secrets in artifacts/errors, unavailable Git, and private download auth/streaming.

- [ ] **Step 2: Run red.** `python3 -m unittest discover -s server -p test_repository_backups.py -q`; expect missing worker/queue failures.

- [ ] **Step 3: Implement queue and secure worker.** Fixed official HTTPS Git remote only; full selected-ref history fetched to private scratch with no checkout/hooks/filters/submodules, credential helper reset, redirects disabled, no credentials in URL/argv. Private askpass can read a child-only environment token. Poll and kill Git process group on configured wall time, disk quota or stop; discard raw subprocess errors. Validate original selected commit exists, create self-contained bundle anchored to it, verify bundle, and ZIP all tracked selected-commit files without trusting export-ignore/subst. Detect LFS pointers/gitlinks and record partial. Never fetch a later head as substitute; ref advancement may only supply ancestors containing the same original SHA. Atomic private publication, hashes and manifest. Respect all global byte/count/storage caps and fixed retry limits. No project execution.

- [ ] **Step 4: Integrate enqueue and metadata/downloads.** Use exact version ownership. Do not widen judge access; judges retain existing pinned Gitee links. Downloads never map arbitrary paths or serve symlinks. Test a queued backup does not block saving or change the original receipt on replay. Mark backup status independently; do not claim PPT/video or runtime dependencies archived.

- [ ] **Step 5: Run green plus Task 1 suites.** Commit an explicit allowlist and report test counts/limits/remaining boundaries.

### Task 3: Frontend version links, seal/backup status, and integration documentation

**Files:** Modify platform.js, platform.css only if necessary, tests/repositories.test.mjs; add focused Node tests if needed. Update docs/repository-provisioning.md, docs/handoff/2026-11-05-gitee-handoff.md and downloads/submission-checklist.txt. Do not regenerate the PPT/PDF unless separately requested.

**Interfaces:**
- Consumes preview/receipt/history `repositorySeal`, `receivedAt`, latest/history `repositoryBackup` from Tasks 1-2.
- No server token or management scope UI. Never say normal accounts were tested. No automatic public publication.

- [ ] **Step 1: Add failing Node regressions.** Sealed preview text explicitly confirms the preview's fixed SHA; old preview remains unchanged. Receipts show server receive vs save times without altering original replay. Status distinguishes Gitee reference seal from independent ready/partial/pending/failed byte backup. Source/download links fixed HTTPS or same-origin safe API URLs, escaped. Test preserving materials on seal setup failure and expired preview. Example:

```javascript
assert.match(markup,/版本已封存/);
assert.match(markup,/独立源码备份/);
assert.ok(!markup.includes('后台令牌'));
```

Use real exported render/controller code through existing vm fixtures, not mirrored implementations.

- [ ] **Step 2: Run red.** `node --test tests/repositories.test.mjs` and the focused new test file.

- [ ] **Step 3: Implement UI.** Add fixed commit-page link next to full SHA using validated GitHub/Gitee/GitCode/GitLab repository parsing; for Gitee prefer seal.viewUrl. Sealed preview states confirmation saves this exact preview; later pushes need another preview to select new code. Keep legacy and GitHub messages. Add private backup metadata/download status in team and admin history contexts without offering unauthenticated public downloads. The original developer branch protection remains separate and may be unverified. Never turn pending/partial into archived success. No new prerequisite file upload.

- [ ] **Step 4: Document operations and evidence.** Explain admin contents API exception, ordinary-account accepted assumption, protected refs/receivedAt/local confirm, Git-dependent asynchronous code-only backup, limits and recovery, private storage and per-version data. Public release remains organizer-confirmed future policy; no new automatic publication. Preserve current formal v1 compatibility and explicit old unarchived records.

- [ ] **Step 5: Run green and static checks.** `node --test tests/*.test.mjs`, `python3 scripts/check_site.py`, `git diff --check`. Commit the exact files and report counts.

### Task 4: Whole-branch verification, PR, deployment and safe live acceptance

**Files:** Release evidence remains in ignored .runtime and work/ outside the Git repository; add only credential-free result summaries to docs when useful.

**Interfaces:** Consumes completed commits and separate code review. Root owns live tokens, SSH and actual provider testing; agents must not inspect them.

- [ ] **Step 1: Independently review the complete branch.** Reviewer checks source against the design, deadline/identity races, recoverable upstream mutation, exact receipt replay, worker subprocess/filesystem/authorization limits and misleading UI. Fix critical/important findings, run covering tests and scoped re-review.
- [ ] **Step 2: Run integrated regression.** `python3 -m unittest discover -s server -q`, `node --test tests/*.test.mjs`, `python3 -m unittest discover -s deploy -p 'test_*.py' -q`, `python3 scripts/check_site.py`, `git diff --check`. Use socket-enabled execution; save safe logs privately.
- [ ] **Step 3: Push explicit branch and PR.** Use codex/gitee-sealed-submissions, preserve source branch and primary checkout unrelated work, no force push. PR documents empirical admin tests and the untested ordinary-account assumption accurately.
- [ ] **Step 4: Rehearse current production database copy and release package.** Existing server /opt/gotc-hackathon only, trusted SSH fingerprint/identity, backup SQLite and environment. Verify server Git installed and data storage limits, configure only new opt-in flags in private EnvironmentFile, preserve old env for rollback. No other services/routes altered.
- [ ] **Step 5: Deploy and accept.** Use official installer and rollback protections. Only test repository/999 noncompeting team, temporarily open submissions with bounded cutoff if needed and restore original settings. Verify protected ref matches SHA, preview/confirm generate new immutable receipt, backup becomes ready/partial accurately, original formal v1 survives, logs contain no token, other services remain active. Verify final confirm's upstream-independent behavior through real local HTTP regressions rather than production firewall changes. Never use real participant/admin accounts as a false ordinary-user pass. Save screenshot of the actual official admin result and JSON evidence outside Git.
