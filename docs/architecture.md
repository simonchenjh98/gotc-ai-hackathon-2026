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

## Network protocols and process boundaries

Nginx terminates public HTTPS and negotiates HTTP/2. It serves the built static site, sends event API requests to the platform Unix socket, and sends `/gotc/v1/` to the gateway Unix socket. Each systemd service runs one Uvicorn worker. The development launcher hosts both ASGI apps on loopback. The production launcher requires a private socket path and a configured HTTPS origin.

| Boundary | Library responsibility | Application responsibility |
| --- | --- | --- |
| Incoming HTTP | Uvicorn handles parsing, framing, keep-alive and flow control. Starlette declares routes and supplies request bodies and responses. | Check the route’s byte limit, identity and revisions. Trust forwarded client addresses only on the configured Unix listener. [Uvicorn settings](https://uvicorn.dev/settings/) · [Starlette routing](https://starlette.dev/routing/) |
| Outbound HTTP | HTTPX manages TLS, connection pools, async request uploads and streamed responses. | Select a configured provider, supply server-owned credentials, enforce admission limits and record the outcome. Repository requests use a bounded synchronous HTTPX client. [HTTPX streaming](https://www.python-httpx.org/async/#streaming-responses) |
| Speech | The ASGI WebSocket interface handles the caller connection. The websockets client opens the provider connection. | Check the team, model, permitted subprotocols, message size, session deadline and speech usage. [Starlette WebSockets](https://starlette.dev/websockets/) · [websockets asyncio client](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html) |
| Streamed events and forms | httpx-sse decodes event fields. python-multipart parses multipart parts incrementally. | Retain original bytes for forwarding. Bound each event and its initial prelude, redact errors, meter completed and partial responses, and spool uploads under the material limits. [WHATWG event streams](https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream) · [Python Multipart API](https://multipart.fastapiexpert.com/api/) |

A team’s chat request enters at `/gotc/v1/chat/completions`. The gateway verifies the key and requested model, admits the call, and rechecks access after waiting. The relay opens the provider request with the organizer’s credential. It forwards server-sent events and accounts for usage even when a caller leaves after inference begins.

New events default to metadata-only gateway logging. Gateway maintenance expires retained content when traffic is idle. Both services expose `/livez`, `/readyz` and `/metrics` to local operational clients. Public Nginx routes exclude those endpoints. [Runtime dependency lock](../backend/requirements.txt)

The organizer dashboard reads event history from SQLite. For live inflight and queued calls it asks the gateway for a bounded snapshot over its private Unix socket after the authenticated database read completes. If the gateway is unavailable, the dashboard labels those counters unavailable. A combined development instance can obtain them from the same process.

## Following a submission

1. The shared browser shell resolves `#team` against the current identity. The route lists the scripts and styles for that workspace. A navigation generation identifies the currently active view, so a delayed load cannot mount the page the user has left.
2. The submission controller handles local editing and renders upload and receipt state. It sends authenticated requests using the shared same-origin client. Scripts use classic browser scope for compatibility with the original controllers. Add cross-feature dependencies to the route declaration and exercise a cold entry in the browser tests.
3. The HTTP layer parses input and enforces origin and size limits. The application dispatches to the submission service. The service checks the team identity, current edit window and revision inside the relevant transaction. A receipt identifies an accepted formal submission.
4. SQLite stores team identity, revisions and receipts. Uploaded materials and database backups use the private data directory. The browser document root contains the site and its authored public assets. Private responses use `no-store`. Public static responses support conditional GET.

## Working with releases

Use the audited running release when a feature in repository history and production differs. The baseline for this reconstruction is commit `8f009f9`. The comparison and tested event rules are recorded in the [platform audit](audits/2026-11-18-platform.md).

For each change, run the affected tests, then `make check` and `make test`. For navigation or identity changes, run the real browser smoke test as well. The operations tests boot an extracted release so missing modules or catalog files fail before deployment. The packaged service reads its persistent database through `GOTC_DATA_DIR`; the public Pages export only needs the public homepage and its dependencies.

The standings benchmark seeds an isolated event and measures 50 authenticated reads following ten warm-ups. To compare with an earlier release containing `server/app.py`, run `python3 tests/performance/standings.py --legacy-root /path/to/release` and `make benchmark` on the same machine. Database timings exclude browser, network and reverse-proxy work.
