# Keep API-key pages bound to the login that opened them

Switching teams in another browser tab replaces the shared session cookie. The
old API-key form currently keeps working and can create a key for the new team.
The cookie is correctly authenticated, but the page's login identity is missing
from the key routes' client and server checks.

## Scope and behavior

- Reuse the existing public `loginId` and `X-GOTC-Login` contract. This marker is
  not a bearer credential and never replaces cookie authentication or team ownership.
- Require the marker on `/api/team/keys` and its subroutes, both reads and writes.
  Missing markers return 428; a marker from another login returns 401. The
  check runs after resolving the authenticated actor inside the existing transaction.
- Bind the API-key card and each request to its opening session and page instance.
  Before actions and after asynchronous responses, check ownership. Invalidate
  stale controls and remove displayed plaintext keys when login changes.
- Keep SDK `/v1/*` bearer-key authentication and gateway billing unchanged.
- Append a focused fix to the student's PR without rewriting its existing commits.

## Verification

Real HTTP tests cover cookie replacement, same-team re-login, missing/empty
markers, list/create/delete/history routes, revoked sessions waiting for a write
lock, existing team ownership, and unaffected SDK access. Browser-side controller
tests cover cross-tab changes, late requests, page replacement, and plaintext cleanup.
Run the complete frontend, backend, deployment, and static checks before pushing.
