# Trusted Roster Automatic Team Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Approve a 3–5-person team automatically only when every declared name and school uniquely matches an organizer-imported trusted relay snapshot and no membership conflict exists.

**Architecture:** Keep the trusted snapshot separate from public registration and generic reference identities. Reuse the existing atomic approval and receipt pipeline, retaining manual review for nonmatches, ambiguous entries and conflicts. Default to disabled until a private snapshot is imported and explicitly enabled.

**Tech Stack:** Python standard library, SQLite, existing plain JavaScript UI, synthetic HTTP and browser fixtures.

## Global Constraints

- No real participant data, attachment contents or credentials in Git or public assets.
- Match existing NFKC/case/whitespace normalization only; no school aliases or fuzzy inference.
- Captain plus every teammate must qualify. Exact duplicates, incomplete or ineligible records remain manual.
- Keep existing admin/current-login/origin checks, membership uniqueness, capacity checks, transaction rollback, receipt privacy and legacy manual approval compatibility.
- Registration does not itself grant automatic-review eligibility; API gateway access remains separately managed.
- Existing-team reuse requires a unique, eligible full roster/captain/school/team-name match; public name alone is insufficient.

## Task 1: Automatic approval vertical slice

- [ ] Add a bounded private trusted snapshot/config and atomic replacement with revision checks through the existing protected roster import ingress.
- [ ] Add authenticated enable/disable controls and private admin summary/import UI.
- [ ] Attempt automatic approval on new applications, pending edits and pending receipt checks, with savepoint fallback to manual review on business conflicts.
- [ ] Keep auto approval audit distinct, preserve bearer redemption, and handle matching registered teams without duplicating them.
- [ ] Add synthetic regression coverage for matching, ambiguity, trust separation, authorization, source changes, races, rollback and legacy behavior.
- [ ] Run frontend and focused backend checks; commit an explicit allowlist and obtain independent review.

## Task 2: Private data preparation and acceptance

- [ ] Parse the supplied latest relay privately, preserving original school spelling and flagging ambiguous rows for manual review.
- [ ] Validate normalized counts, duplicates and snapshot digest without publishing identities.
- [ ] Exercise the real browser with synthetic matching and nonmatching teams, including five people and existing registrations.
- [ ] Update the existing open PR and verify CI. Keep deployment/import state explicit; never claim unperformed production activation.
