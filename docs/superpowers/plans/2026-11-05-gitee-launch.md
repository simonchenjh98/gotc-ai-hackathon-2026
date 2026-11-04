# Gitee public launch and submission entry

User explicitly authorized on 2026-11-05: create/use the seven-day projects + mandatory user_info Gitee token, configure the event server, test and launch the workflow, update the homepage submission entry, and open a PR. The credential stays in ignored local/server private files; never read or print it in agents, reports, logs, git, commands, screenshots, or tests. GitHub organization integration remains deferred; GitHub is still the source host/PR destination.

## Task 2: Public request safeguards and homepage entry

Implement in the existing isolated checkout `/Users/xinlu/Documents/ChatGPT/GOTC/gotc-ai-hackathon-2026/.runtime/worktrees/repository-provisioning`, branch `codex/gitee-repository-submissions`. This branch starts at latest `upstream/main` (0282853). A cherry-pick of only our feature commits is currently in progress, with a platform.js conflict while applying5956a59. Complete it preserving upstream GitCode URL/commit support and all repository features. Remaining commits in the active sequence are00f62e8,7690e33,52c5f5a. Do not include unrelated participant-showcase docs or primary-checkout edits. Do not read `.runtime/` or any secrets; parent owns live credentials and deployment.

### Files and interfaces

- `server/repositories.py`: bounded shared provider operations, per-team preview/confirmation request frequency. Preserve exact successful request replay before any provider work.
- `server/test_repositories.py`: behavioral quota, concurrency release, no writer-lock waiting, cross-team fairness, old receipt replay and upstream errors.
- `index.html`, `downloads/submission-checklist.txt`: visible current Gitee workflow and working entry `https://8.141.114.201/gotc/platform.html#team`; don't invent a same-domain platform route (etownyouthai.com is static Pages and `/platform.html` is404). Entry label `申请 Gitee 仓库 / 提交作品 ↗`; navigation can retain the materials section anchor. State team login, organization application, automatic SHA only for bound teams, preserve legacy manual mode and no-force-push rule.
- `docs/repository-provisioning.md`, optional focused Node tests: document actual safeguards and accurate configuration, preserve enterprise-only API routing and exact push-only evidence.

### Required behavior and verification

- [ ] Complete cherry-picks with both upstream GitCode and our Gitee enterprise workflow intact. `git diff upstream/main` must contain only task changes; preserve primary working tree.
- [ ] Add a single-instance per-provider operation concurrency ceiling of4. Acquire without holding the SQLite writer lock and before changing persistent provision state; saturation returns a safe503/busy result without implying remote work occurred. Release in finally on every path. Avoid nested acquisition within one operation.
- [ ] Limit each authenticated team's fresh SHA previews to10 per rolling60 seconds and fresh confirmation upstream verifications to20 per rolling60 seconds. Limits are independent per team/provider, bounded in-memory state with monotonic time, thread-safe. Reject with429 and actionable wait/recheck guidance; exact successful request replay must remain successful without spending upstream quota. No candidate token values or user-supplied hosts.
- [ ] Tests prove limits, independent teams, reset after interval, saturation without remote calls/state change, error releases permit, and immutable receipt replay. Prefer injected clocks/barriers over sleep.
- [ ] Update homepage label/instructions and checklist as above, with the actual current platform address. Preserve all recently merged GitCode support, scoring, gateway and receipt behavior.
- [ ] Run relevant Python repository/submission/receipt tests, full Node suite, static site checks, and deployment tests. Run full server regression once for the integrated latest-upstream candidate. No live calls, browser credential work, deployment, push, or PR creation by this implementer; parent owns these.
- [ ] Commit exact changes and report tests plus diff paths. Report any design ambiguity promptly; don't introduce new frameworks or restructure unrelated modules.

Parent handles SSH preflight/backup, private credential configuration, real upstream acceptance in separate synthetic data, production deployment, public browser verification, handbook refresh, source push and PR. Reviewer must approve this production guard/entry task before release.
