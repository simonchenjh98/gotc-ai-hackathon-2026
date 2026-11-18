# Release gates, 18 November 2026

## Local checks

The local host was macOS 27.0.1 on Apple Silicon. The HTTP and type checks used CPython 3.12.12 with the locked application dependencies. Ruff was 0.14.10 and mypy was 1.19.0, matching the CI tool pins. All network tests used a temporary certificate, a loopback listener and synthetic private Unix sockets.

The release manifest test initially reported missing entries for `backend/gotc/gateway/admission.py`, `backend/gotc/password_capacity.py` and `frontend/static/platform/login-retry.js`. After those entries were added, the installer test reached its resource assertions and reported that `LimitNOFILE=1024` was missing. Both checks passed after the unit template was updated.

```text
$ python -Bm unittest discover -s tests/operations -p test_release_security.py -k test_release_manifest_includes_each_application_module
Ran 1 test in 0.004s
OK
$ python -Bm unittest discover -s tests/operations -p test_installer_recovery.py -k test_normal_isolated_install_checks_both_sockets_and_individual_neighbors
Ran 1 test in 0.210s
OK
$ make check
Application layout and backend dependency boundaries passed.
Public resources, anchors, schedule panels and HTTPS links: passed
```

The Nginx executable was added to the test process's PATH. The certificate generator was LibreSSL 3.3.6. The first sandboxed attempt passed three configuration tests and failed both live tests with a permission error at the loopback bind. The authorized loopback run produced the following output.

```text
$ nginx -v
nginx version: nginx/1.28.2
$ python -Bm unittest discover -v -s tests/operations -p test_nginx.py
test_https_negotiation_static_headers_and_shared_host_isolation ... ok
test_same_size_release_switch_returns_current_html_and_manifest ... ok
test_dedicated_host_and_socket_routing ... ok
test_replacement_preserves_every_byte_outside_the_markers ... ok
test_validates_named_https_host_and_http2_before_changing_it ... ok
Ran 5 tests in 0.357s
OK
$ make operations-test VENV=.runtime/protocol-venv
Ran 27 tests in 7.005s
OK
```

The new Ruff gate checks all Pyflakes and bugbear rules plus blocking HTTP, file open and sleep calls in async functions. Mypy checks bodies of unannotated functions throughout HTTP, organizer authentication, credentials and the session service. The two admission modules use strict type checking. The scoped mypy commands use `--follow-imports=silent` so imported local definitions supply type information while independent legacy modules remain outside the reported target. One error-code-specific suppression in the HTTP cookie writer preserves the existing tested `SameSite=Strict` header casing. The workflow installs the locked runtime dependencies so Starlette, pyotp and AnyIO annotations remain available.

```sh
auth="backend/gotc/admin_auth.py backend/gotc/service.py backend/gotc/credentials.py"
admission="backend/gotc/gateway/admission.py backend/gotc/password_capacity.py"
ruff check --target-version py311 --select E9,F63,F7,F82,B012 --exclude 'tests/backend/fixtures' backend ops tools tests
ruff check --target-version py311 --select F,B,ASYNC210,ASYNC230,ASYNC251 backend/gotc/http $auth $admission
mypy --strict ops/deploy/nginx.py tools/build_frontend.py
mypy --follow-imports=silent --check-untyped-defs --warn-unused-ignores backend/gotc/http $auth
mypy --follow-imports=silent --strict $admission
```

```text
All checks passed!
All checks passed!
Success: no issues found in 2 source files
Success: no issues found in 10 source files
Success: no issues found in 2 source files
```

## Linux rehearsal

The local Docker socket was absent. The fork's Actions API reported `{"allowed_actions":"all","enabled":true}`. A push to `codex/platform-rebuild` triggers two independent Ubuntu checks.

| Check | Linux runner exercise |
| --- | --- |
| Nginx config and HTTPS asset contract | Installs Nginx, reports Nginx/OpenSSL versions and runs all five edge tests in verbose mode. |
| Isolated services and Git under systemd | Installs the locked interpreter environment from wheels, starts both generated service units with disposable identities and an empty data directory, verifies /readyz through each Unix socket as the proxy group, reports systemd resource limits and runs the real Git backup suite under the platform service identity and sandbox. |

The protected-main signing and Pages workflows exclude this rehearsal branch. For the exact Linux run and deployment acceptance status, see the [release blocker audit](2026-11-18-release-blockers.md). The hosted runner exercises systemd and Git on a clean machine. Shared-host occupancy, cgroup memory pressure and operator-owned provisioning still require a production-like rehearsal. The documented socket budget and the synthetic process measurements apply to the default service configuration. [Service capacity](../../ops/deploy/README.md) · [Proxy capacity](../../ops/deploy/nginx.md) · [80-stream results](2026-11-18-gateway-capacity.md)
