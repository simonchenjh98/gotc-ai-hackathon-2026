# Task 2 report: public request guards and Gitee entry

Status: DONE. Implementation complete; all requested offline checks passed.

## Commits and integration

- Base: upstream/main 0282853; branch codex/gitee-repository-submissions.
- Active cherry-pick completed: 239ab57 (5956a59), e06d6df (00f62e8), 9a1e133 (7690e33), 5277a76 (52c5f5a).
- Task guard/entry commit: 32e9a8b.
- platform.js conflict retained upstream GitCode repository/commit help and PPT/video share-link guidance, plus the complete repository feature.
- No unrelated participant files were included; primary checkout untouched. No push, live API call, deployment, credential/config inspection, or SSH action performed.

## Changes

- ProviderOperations shares four nonblocking permits per fixed provider across approve/reconcile/preview/confirmation. Provision obtains its permit outside every SQLite writer transaction, before persistent state changes; atomic state/revision/identity checks run again afterwards. Context-manager finally releases on every exit, without nested acquisition.
- Authenticated fresh preview and confirmation checks have independent rolling monotonic windows: 10 and 20 per 60 seconds respectively, keyed by team/provider/action. A lock protects quotas, deque sizes cannot exceed limits, stale windows are removed, and at most 4096 active windows persist; full capacity safely rejects new keys without evicting still-limited teams. Upstream failures count, busy rejection does not.
- Successful exact receipt replay retains its existing early bypass of upstream work and quotas. A full quota, saturated provider, expired preview, or closed window cannot alter its immutable receipt.
- 429 and repository_busy responses preserve editable materials in the team form, clear stale confirmation UI, and give wait/recheck guidance. Other 503/network failures retain uncertain-result recovery.
- Homepage button is exactly “申请 Gitee 仓库 / 提交作品 ↗” at https://8.141.114.201/gotc/platform.html#team. Homepage/checklist explain team login, application/review, automatic SHA only for bound teams, legacy manual mode, and no-force-push rules. Documentation retains enterprise-only Gitee routing and exact push evidence.

## Verification

- `python3 -m unittest discover -s server -p test_repositories.py -q`: 31 tests passed (16.414 s). The initial restricted-sandbox attempt could not bind localhost; the authorized socket-enabled run passed.
- `node --test tests/*.test.mjs`: 203 tests passed, 0 failed (4.685 s).
- `node --test tests/repositories.test.mjs`: 13 tests passed after narrowing repository-specific error handling (4.555 s).
- `python3 scripts/check_site.py`: public resources, anchors, schedule panels and HTTPS links passed.
- `git diff --check`: passed.
- `python3 -m unittest discover -s server -q`: full integrated server regression passed, 208 tests in 156.494 s, including submissions, receipts, gateway and deployment configuration. Only offline synthetic data was used; expected injected-fault traces and a ResourceWarning appeared in the log.
- Added tests exercise rolling limits/reset, authenticated other-team fairness, preview/confirmation and provider independence, quota thread safety/bounded retention, four simultaneous permits via barrier/events without sleeps, no writer lock during permit acquisition/upstream work, no calls or persistent changes on saturation, error releases, and immutable replay after quota saturation/expiry.

## Diff paths

Task commit: docs/repository-provisioning.md; docs/superpowers/plans/2026-11-05-gitee-launch.md; downloads/submission-checklist.txt; index.html; platform.js; server/repositories.py; server/test_repositories.py; tests/repositories.test.mjs.

Feature branch additionally contains only original repository feature paths: docs/superpowers/plans/2026-11-04-repository-provisioning.md; docs/superpowers/specs/2026-11-04-repository-provisioning-design.md; platform.css; server/app.py; server/submission_records.py.

## Concerns and remaining acceptance

- Quotas are intentionally single-instance memory state and reset on process restart. Multiple workers/instances require shared protection.
- No live Gitee, production, public browser, certificate, branch-protection or archive claims are made by these offline checks. Parent owns those checks, final PDF, push and PR.
- Independent task/branch review remains a parent gate before release.
