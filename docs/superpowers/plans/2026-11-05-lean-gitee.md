# Lean Gitee submission implementation plan

**Goal:** Use the existing Gitee enterprise API and team binding to minimize access and duplicate material entry. Keep independent archives available only for prior jobs when the operator disables future backups.

## Global Constraints

Current checkout only; use no private runtime files, secret, remote mutation or server login during implementation. Team identity/approval, replay, SHA sealing and existing archives remain authoritative. Privilege fallback must be based on explicit membership-required evidence, not a generic error. See the accepted spec in docs/superpowers/specs/2026-11-05-lean-gitee-design.md.

### Task 1: Implement and test the integrated submission contract

- [ ] server/repositories.py: prioritize repository push; validate username/id/effective permission; only on explicit requirement check enterprise membership and use POST /enterprises/{enterprise}/members with username and role=member. Handle pending/accepted/unknown, bounded safe classification, no repeated blind invitations, no false ready. Communicate actionable states to team/admin. No credentials in text.
- [ ] server/app.py, submission_records.py, repositories.py, platform.js: on an authenticated Gitee binding omit repeated README, license + license URL, sources text. Point readers at materials inside the selected SHA (avoid claiming a license exists); do not weaken manual/GitHub checks or alter legacy receipts. Check local draft collection, recovery, validation, preview and admin/judge render. Keep known limitations expressible, e.g. in the repository README.
- [ ] server/repository_backups.py: honor existing explicit enable flag when enqueueing as well as starting. New sealed no-job case should clearly say Gitee version sealed; old downloads/roles remain intact. Adapt backup fixtures to enable explicitly. Remove misleading backup tasks for new contestants but keep historical access.
- [ ] Focused regression tests for the above; self-review and commit explicit file allowlist. Whole suites and released-state verification belong to operator.
