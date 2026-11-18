# GOTC 2026 Open Source AI Hackathon

The public site publishes the schedule, team directory and contestant guidance. The event platform handles team registration, submissions, judging, voting, finals, organizer operations and team AI access. The participant interface is in Chinese. [Public event site](https://etownyouthai.com/)

## Local development

Use Python 3.11 or later and Node.js 22 or later. From the repository root, run:

```sh
make dev
```

Open <http://127.0.0.1:5274/gotc/>. The development launcher serves the platform and gateway together on loopback. The first run creates a private SQLite database and an organizer bootstrap credential. Local state defaults to `${XDG_DATA_HOME:-$HOME/.local/share}/gotc`. Set `GOTC_DATA_DIR` explicitly to reuse an event. If startup detects an older data directory, verify the path before proceeding.

Production runs separate Starlette apps on Uvicorn Unix sockets behind an HTTPS Nginx entry point. HTTPX connects to external providers. Private gateway content capture is opt-in for new events. [Architecture](docs/architecture.md) · [Organizer account migration](docs/organizer-accounts.md)

The [hardening report](docs/audits/2026-11-18-hardening.md) maps the rebuild brief to the implementation, synthetic benchmarks and remaining rollout checks.

## Project layout

| Directory | Responsibility |
| --- | --- |
| `frontend/` | Public pages, team directory, downloadable materials and browser workspaces |
| `backend/gotc/` | Platform and gateway APIs, data migrations, event workflows and model accounting |
| `ops/` | Artifact building, offline installation, Nginx integration and backup operations |
| `tests/` | Frontend, backend, browser and release tests, plus synthetic performance measurements |
| `tools/` | Checks and asset generation shared by development and CI |
| `docs/` | Engineering guides, audits and dated design records |

## Event contracts

A formal submission requires the presentation and project ZIP. Teams can recover drafts and retrieve a submission receipt. Teams see their own available scores. Organizers control the all-team professional standings. Roadshow publication normally supplies the finals roster, with an explicitly confirmed roster taking precedence. Finals use seven judge seats. Team API credentials and provider credentials have distinct scopes.

## Verification and packaging

```sh
make check
make test
make benchmark
```

`make test` covers browser-side logic, backend behavior and release packaging. The separate browser suite exercises real Chromium and synthetic event accounts. [Browser test instructions](tests/browser/README.md)

To produce a local release archive from a clean checkout, set the approved public origin as described in the deployment guide, then run:

```sh
make wheels
python3 -m ops.deploy.deploy --package /tmp/gotc-release.tar.gz
```

The runtime lock pins dependency versions and wheel hashes. Production installation uses the wheels included in the release. [Runtime lock](backend/requirements.txt) · [Deployment guide](ops/deploy/README.md) · [Backend guide](backend/README.md)

Code is licensed under the MIT License. Rights to event artwork, names and marks belong to their respective owners.
