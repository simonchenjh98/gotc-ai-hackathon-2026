# Repository Provisioning Implementation Plan

> For agentic workers: use subagent-driven-development to implement the following vertical task and review it independently.

**Goal:** Team-bound organization repository requests, administrator-approved provisioning, and server-resolved SHA preview/confirmation using existing formal submission receipts.

**Architecture:** Add one focused `server/repositories.py` module for provider clients, persistent workflow and preview records; integrate with `server/app.py` and its existing submission transaction/receipt logic. Add the team repository panel and administrator request handling to existing plain-JS UI without changing unrelated scoring behavior.

**Tech Stack:** Python 3.11+ standard library, SQLite, vanilla JS, Node/Python tests.

## Global Constraints

- Canonical public website: https://etownyouthai.com/ . No credentials in code, docs, screenshots or reports.
- Existing checkout at `/Users/xinlu/Documents/ChatGPT/GOTC/gotc-ai-hackathon-2026` contains unrelated work. Edit only this isolated worktree.
- Fixed upstream hosts api.github.com and gitee.com/api/v5; never fetch user-supplied URLs. Do not perform live external writes or create tokens during implementation.
- Team identity always comes from authenticated server context. Human admin verifies provider usernames before approving repository invitations.
- Preserve old unbound-team submissions and existing receipt, history, conflict, deadline and scoring semantics.
- No claim of independent code archival or provider integration until actually verified.

## Task 1: Repository request through immutable submission receipt

**Files:** Create `server/repositories.py`, `server/test_repositories.py`, `tests/repositories.test.mjs`, `docs/repository-provisioning.md`; modify `server/app.py`, `platform.js`, `platform-admin.js` only if needed, `platform.css`, deployment packaging allowlists if new static files require them.

**Consumes:** Existing authenticated `team_accounts`, administrator role checks, `submissions`, and `SubmissionRecords` transaction/receipt helpers. Inspect their actual interfaces before implementation.

**Produces:** Team and admin JSON routes under the existing API prefix for provider availability, application, approval/provisioning status and short-lived SHA preview; existing submission POST consumes a preview identifier for bound teams. Use `GOTC_REPO_GITHUB_ORG=GOTC-AI-HACKATHON`, `GOTC_REPO_GITHUB_TOKEN`, `GOTC_REPO_GITEE_ORG=gotc-ai-hackathon`, `GOTC_REPO_GITEE_TOKEN`, and explicit Gitee enablement. Tokens are only read server-side, never returned or logged.

- [ ] Run baseline existing submission/receipt tests. Record baseline failures independently of new work.
- [ ] Add focused provider/workflow tests: disabled configuration, strict usernames and generated slugs, cross-team denial, team-only request and admin-only approval, pending invitation state, retry with known external identity, rejection of unrelated existing repo, upstream timeout, no credentials in JSON/errors.
- [ ] Implement persistent application/binding/provisioning state with bounded upstream requests outside SQLite writer transactions, fixed allowed origins and secret-redacted errors. Follow the design's recovery semantics.
- [ ] Add tests for SHA preview tied to team/session/binding revision and expiry, changed upstream head, caller-provided code override, replay of successful existing request and closed/deadline window.
- [ ] Connect preview verification to the existing formal submission path. Server-supplied code URL/SHA must reach the same atomic receipt/history transaction as current formal materials. Network uncertainty must not turn a known previous success into failure.
- [ ] Add team application/status/bound repo panel. Bound teams read the repo and branch, obtain SHA when checking materials, preview full SHA, and confirm with the preview ID. Do not retain required hidden code input errors; keep unbound legacy submission behavior intact.
- [ ] Add administrator pending list, explicit username-verification acknowledgement and approve/reconcile actions, with helpful disabled/unconfigured messages. Do not add insecure client-side token settings.
- [ ] Document exact environment variables, least privilege requirements, API/enterprise compatibility caveat, force-push rule, manual reconciliation, and local versus live acceptance status.
- [ ] Run focused tests, complete site Node tests and Python regressions including deployment; perform isolated browser QA on team/application/bound/preview states using synthetic data.
- [ ] Self-review diff for security, transaction locks, recovery and conflicts. Commit only intended changes and write a report including all test commands, unresolved limitations and exact changed paths.

## Review

Dispatch an independent task reviewer against the complete feature diff and design. Fix load-bearing findings, run focused regressions and request a scoped re-review. Only integrate after the reviewer approves and all relevant checks pass. Live provisioning remains gated on separately configured organization credentials and actual permissions.
