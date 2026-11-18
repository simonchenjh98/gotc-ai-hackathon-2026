# HTTPS edge for GOTC

Configure one exact HTTPS origin, including its port when nonstandard. Pass that same origin to the release builder, installer and both service units. The hostname or IP address must already belong to an explicitly named TLS virtual host. The handler accepts an existing `listen ... ssl http2` listener or a TLS listener with the server directive `http2 on`. It rejects an ambiguous virtual host, a managed block in another host and a target virtual host that also listens for plaintext HTTP.

## Host preparation

Nginx declares request-rate zones and the WebSocket connection map in its `http` context. As part of one-time host provisioning, generate the contents of a dedicated `conf.d/gotc-limits.conf` include with the following command. Review it before installing it. The normal application deployment only replaces GOTC's marked location block in the selected HTTPS virtual host.

```sh
python3 -m ops.deploy.nginx http-context
```

For a dedicated host, choose the public domain, obtain its certificate and key, and supply those values to the generator. This command prints the proposed server block. The operator controls DNS and certificate provisioning. HTTPS responses for DNS hostnames advertise one year of HSTS, matching the application. Browsers apply HSTS to the whole hostname. IP-hosted releases omit that header.

```sh
python3 -m ops.deploy.nginx dedicated-host \
  --public-origin "$GOTC_PUBLIC_ORIGIN" \
  --certificate "$TLS_CERTIFICATE" --certificate-key "$TLS_PRIVATE_KEY"
```

Nginx must be able to traverse the runtime socket directories and connect to their owner-configured group sockets. The platform socket is `/run/gotc-platform/http.sock` and the SDK gateway socket is `/run/gotc-gateway/http.sock`. The application units use one worker each. Their readiness, liveness and metrics endpoints are available to the local supervisor on the sockets.

## Request handling

| Path under `/gotc/` | Handler |
| --- | --- |
| Home, teams, platform, manifest and approved static/download extensions | Nginx reads the assembled release's `current/frontend/` directory. HTML, the manifest and other unhashed files return the current bytes when a browser revalidates. Deterministic releases share file timestamps, so the edge suppresses size-and-time ETags and ignores date validators for these URLs. Content-hashed JS and CSS use one-year immutable caching and support conditional requests. Eligible responses use gzip. |
| `api/` and `materials/` | Platform Unix socket with bounded bodies. Native material downloads and HTML previews retain the application's authenticated streaming and response-level security headers. |
| `v1/` | Gateway Unix socket with streaming and WebSocket upgrade support. |

The edge generates the request ID and replaces the forwarded client address and scheme. Neighboring sites do not use GOTC's per-IP rate zones. Static files are restricted to their documented paths and file types. Symlink traversal under the frontend directory is disabled. The configured document root may itself traverse the root-owned `current` release pointer.

The CI edge check validates a temporary Nginx configuration, negotiates HTTP/2 using a disposable certificate, calls synthetic socket upstreams, and exercises HTML caching, gzip, immutable assets, private paths and neighboring routes.
