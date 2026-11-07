# Team submission files implementation plan

**Goal:** Make submission visible immediately after team login, with separate PPT, code and supporting files, and directly viewable showcase sections. Name uploaded artifacts `队名+作品名称.ext`.

**Architecture:** Retain versioned drafts, repository SHA sealing, formal confirmation and receipts. Upload immutable files separately into the private data directory and place their public capability URLs into the versioned material fields. Serve HTML with an opaque-origin CSP sandbox; serve other files as attachments.

**Tech stack:** Existing vanilla JavaScript, Python HTTP server, SQLite, nginx and automated Node/Python tests.

## Contract

- POST `/api/team/uploads/{slides|files|demo}` accepts binary data with existing session, Origin and login marker plus percent-encoded `X-GOTC-Project` and `X-GOTC-Filename` headers. Server derives the team name from the authenticated team.
- Slides: PPT/PPTX/PDF, 50 MiB. Supporting files: ZIP, 100 MiB. Showcase: self-contained HTML/HTM, 10 MiB. Total team storage: 500 MiB. Upload errors preserve the current draft.
- Return `{url, filename, size, kind}`. Map to `slidesUrl`, optional `filesUrl`, and `demoUrl`. Repository code and fixed commit remain required. A showcase URL is required for formal submissions; video is optional.
- Upload is preparation, not formal submission. Existing save, preview, confirmation and receipt remain the completion criteria. Do not change the November 8, 13:00 Beijing deadline.
- Public files are immutable and use unguessable URLs. Never serve arbitrary data-directory paths. Validate ownership and material kind when saving internal upload URLs. Preserve existing external HTTPS links and historical snapshots.
- Normalize unsafe filename characters and retain the extension. ZIP member names and project directory structure remain intact, so source code and relative imports keep working.

## Work and verification

1. Backend: add `server/material_uploads.py`, integrate authentication, public downloads and material validation in `server/app.py`, and add a scoped upload nginx location in `deploy/install.py`. Test real HTTP upload/download, identity, CSRF, ownership, size/quota, deadline, filename and HTML sandbox responses.
2. Frontend: update `platform.js` and `platform.css` with a prominent entry, three independently labelled sections, upload progress/errors, filename preview and open/download links. Preserve draft recovery and reject submission while uploads are pending. Test interaction/race handling and formal validation.
3. Documentation: update the participant guide, PDF and submission checklist to match the three sections, upload sizes and naming. Clearly distinguish file upload, repository push, draft save and formal receipt.
4. Integration: run Node tests, server tests, deployment tests, static-reference validation and repository hygiene. Review lifecycle, snapshots, CSV and access-check projections for the new field.
5. Release: integrate current upstream, publish through the existing release process, check live version and upload UI. Do not submit synthetic work under a real team or modify existing event submissions for testing.
