# How the GOTC platform fits together

The event has two delivery targets. GitHub Pages hosts the public information site. The event host runs the full application behind Nginx. Both use the same homepage source. A local developer starts the event host with `make dev` and can follow the same links and API paths as a contestant.

## Where to change a feature

| Responsibility | Entry point | Example |
| --- | --- | --- |
| Public pages | `frontend/index.html`, `frontend/teams.html`, `frontend/static/site/`, `frontend/static/registration/` | Schedule, directory, public downloads, shared navigation |
| Platform navigation | `frontend/platform.html`, `frontend/static/platform/routes.js`, `frontend/static/platform/navigation.js` | Map a hash, role and source dependencies to a view |
| Platform views | Named directories below `frontend/static/platform/` | A presentation upload belongs in `submissions/`. Finals scoring belongs in `finals/`. |
| HTTP | `backend/gotc/http/` | Interpret the method and path, check request limits, serve permitted public files and write HTTP responses |
| Application routing | `backend/gotc/api.py`, `backend/gotc/service.py` | Bind a request to the relevant domain, authorize the actor and preserve the transaction boundary |
| Domain operations | `backend/gotc/teams/`, `submissions/`, `judging/`, `gateway/` | Membership, receipts, scoring, model calls and accounting |
| Persistence and startup | `backend/gotc/database.py`, `bootstrap.py`, `cli.py` | Schema compatibility, connections, worker startup and shutdown |
| Operations | `ops/deploy/`, `ops/backup_event.py` | Build a release archive, stage Pages, or back up the configured data directory |
| Verification | `tests/frontend/`, `tests/backend/`, `tests/browser/`, `tests/operations/`, `tests/performance/` | Controller checks, HTTP contracts, real browser journeys, package rehearsal and repeatable timings |

## Following a submission

1. The shared browser shell resolves `#team` against the current identity. The route lists the scripts and styles for that workspace. A navigation generation identifies the currently active view, so a delayed load cannot mount the page the user has left.
2. The submission controller handles local editing and renders upload and receipt state. It sends authenticated requests using the shared same-origin client. Scripts use classic browser scope for compatibility with the original controllers. Add cross-feature dependencies to the route declaration and exercise a cold entry in the browser tests.
3. The HTTP layer parses input and enforces origin and size limits. The application dispatches to the submission service. The service checks the team identity, current edit window and revision inside the relevant transaction. A receipt identifies an accepted formal submission.
4. SQLite stores team identity, revisions and receipts. Uploaded materials and database backups use the private data directory. The browser document root contains the site and its authored public assets. Private responses use `no-store`. Public static responses support conditional GET.

## Working with releases

Use the audited running release when a feature in repository history and production differs. The baseline for this reconstruction is commit `8f009f9`. The comparison and tested event rules are recorded in the [platform audit](audits/2026-11-18-platform.md).

For each change, run the affected tests, then `make check` and `make test`. For navigation or identity changes, run the real browser smoke test as well. The operations tests boot an extracted release so missing modules or catalog files fail before deployment. The packaged service reads its persistent database through `GOTC_DATA_DIR`; the public Pages export only needs the public homepage and its dependencies.

The standings benchmark seeds an isolated event and measures 50 authenticated reads following ten warm-ups. To compare with an earlier release containing `server/app.py`, run `python3 tests/performance/standings.py --legacy-root /path/to/release` and `make benchmark` on the same machine. Database timings exclude browser, network and reverse-proxy work.
