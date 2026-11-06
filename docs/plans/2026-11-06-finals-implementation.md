> 2026-11-06 后续调整：按用户在网站维护会话的明确要求，决赛取消邀请码和登录门槛；评委直接选择自己的姓名或席位评分。以下早期方案中有关评委专属码的设计已由免邀请码入口替代，管理员权限保持不变。详见 docs/finals.md。

# Finals Scoring Implementation Plan

> Execute the approved feature in this chat; preserve concurrent maintenance work.

**Goal:** Implement eight-judge finals scoring, six nomination awards, one-award-per-team rollover, and live administrator results.

**Architecture:** Separate finals tables and service inside the existing transaction/authentication boundary. Separate browser module with judge, admin and published-board routes, using existing session ownership and styling.

**Tech Stack:** Python standard library, SQLite, vanilla JavaScript, Node test runner, unittest.

---

1. Create `server/finals.py` and `server/test_finals.py`. Test independent records, numeric validation, strict page ownership, concurrent version writes, eight-judge completion, abstention, ties, award limits, publication and restart persistence with synthetic databases.
2. Integrate service import, initialization, GET read transactions, POST dispatch and static allowlist in `server/app.py` without altering legacy calculations.
3. Create `platform-finals.js`, `platform-finals.css`, and `tests/finals.test.mjs`. Implement judge score forms, independent nomination ballot, admin roster/settings/credential controls, live statistics and expandable award candidates with team number/name/votes.
4. Add routes `judge-finals`, `admin-finals`, `board-finals` and captured-login headers in `platform.js`; add script, styles and navigation in `platform.html`; update `scripts/check_site.py` route validation.
5. Add assets to `deploy/install.py` allowlist; verify `deploy/deploy.py` packages the new modules and no credentials.
6. Run targeted checks, review implementation independently, then run all Node and backend regressions plus deploy recovery/security checks appropriate to the platform. Reconcile latest maintenance commits before any release.
7. Document controls, confirmed scoring/award policy, actual deployment state, and tested limits in `docs/finals.md`.
