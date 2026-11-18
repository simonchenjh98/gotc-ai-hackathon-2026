"""A disposable production-entrypoint pair, or the archived one-process reference."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import http.client
import json
import os
from pathlib import Path
import resource
import signal
import socket
import subprocess
import sys
import tempfile
import time

BASE = "/gotc"
ORIGIN = "https://synthetic-event.invalid"
SLOTS = 2


class UnixClient(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


@dataclass
class Client:
    destination: int | Path
    session: dict = field(default_factory=dict)

    def request(self, method, path, body=None, headers=None, base=BASE):
        conn = (
            http.client.HTTPConnection("127.0.0.1", self.destination, timeout=10)
            if isinstance(self.destination, int)
            else UnixClient(self.destination)
        )
        try:
            merged = {
                "Host": "synthetic-event.invalid",
                "Origin": ORIGIN,
                **self.session,
                **(headers or {}),
            }
            if isinstance(body, bytes):
                payload = body
            else:
                payload = None if body is None else json.dumps(body).encode()
                if body is not None:
                    merged["Content-Type"] = "application/json"
            conn.request(method, base + path, payload, merged)
            response = conn.getresponse()
            return (
                response.status,
                {k.lower(): v for k, v in response.getheaders()},
                response.read(),
            )
        finally:
            conn.close()

    def json(self, path, body=None, expected=200, **kwargs):
        status, headers, raw = self.request(
            "GET" if body is None else "POST", path, body, **kwargs
        )
        if status != expected:
            raise RuntimeError(f"{path} returned {status}, expected {expected}")
        value = json.loads(raw)
        if path == "/api/login" and "loginId" in value:
            self.session = {
                "Cookie": headers["set-cookie"].split(";", 1)[0],
                "X-GOTC-Login": value["loginId"],
            }
        return value


def child(kind, source, work):
    sys.path.insert(0, str(source))
    # Fixture size is fixed and small on both revisions. There are no waiters.
    from backend.gotc.gateway import keys

    if os.environ.get("GOTC_QA_PEAK_RSS") and kind == "gateway":
        def write_peak(*_):
            units = 1 if sys.platform == "darwin" else 1024
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * units
            (work / "gateway-peak").write_text(str(peak))
        signal.signal(signal.SIGUSR1, write_peak)
    else:
        keys.TEAM_CONCURRENCY = SLOTS
        if hasattr(keys, "GLOBAL_RUNNING"):
            keys.GLOBAL_RUNNING = SLOTS
    if kind == "baseline":
        from backend.gotc.http.server import EventHTTPServer
        from backend.gotc.service import EventService

        service = EventService()
        with EventHTTPServer(("127.0.0.1", 0), service) as server:
            (work / "port").write_text(str(server.server_port))
            server.serve_forever()
    else:
        import runpy

        sys.argv = [
            "backend.gotc",
            "--service",
            kind,
            "--uds",
            str(work / (kind + ".sock")),
        ]
        runpy.run_module("backend.gotc", run_name="__main__")


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=7)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


@contextmanager
def event(source, layout, *, gateway_measurement=None):
    with tempfile.TemporaryDirectory(
        prefix="gotc-qa-", dir="/tmp"
    ) as directory, ExitStack() as stack:
        work = Path(directory)
        private = work / "data"
        private.mkdir(mode=0o700)
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}
        env.update(
            GOTC_PUBLIC_ORIGIN=ORIGIN,
            GOTC_DATA_DIR=str(private),
            GOTC_BASE_PATH=BASE,
            PYTHONDONTWRITEBYTECODE="1",
            NO_PROXY="127.0.0.1,localhost",
            GOTC_GATEWAY_SOCKET=str(work / "gateway.sock"),
        )
        if gateway_measurement is not None:
            env["GOTC_QA_PEAK_RSS"] = "1"
        def peak_rss():
            peak = work / "gateway-peak"
            peak.unlink(missing_ok=True)
            process.send_signal(signal.SIGUSR1)  # The last process in the pair owns the gateway.
            cutoff = time.monotonic() + 2
            while time.monotonic() < cutoff:
                if peak.exists() and (value := peak.read_text()).isdigit():
                    return int(value)
                time.sleep(0.01)
            raise RuntimeError("Gateway RSS sample timed out")
        endpoints = {}
        for kind in (["baseline"] if layout == "baseline" else ["platform", "gateway"]):
            diagnostic = stack.enter_context((work / (kind + ".stderr")).open("w+b"))
            process = subprocess.Popen(
                [sys.executable, __file__, kind, str(source), directory],
                env=env,
                cwd=source,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=diagnostic,
            )
            stack.callback(stop, process)
            marker = work / ("port" if kind == "baseline" else kind + ".sock")
            deadline = time.monotonic() + 15
            status = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    diagnostic.seek(0)
                    detail = (
                        diagnostic.read().decode(errors="replace").splitlines()[-1:]
                    )
                    raise RuntimeError(
                        f"{kind} exited during startup ({process.returncode}): {detail}"
                    )
                if marker.exists():
                    destination = (
                        int(marker.read_text()) if kind == "baseline" else marker
                    )
                    try:
                        client = Client(destination)
                        status, _, _ = (
                            client.request("GET", "/api/session")
                            if kind == "baseline"
                            else client.request(
                                "GET", "/readyz", headers={"Host": "localhost"}, base=""
                            )
                        )
                        if status == 200:
                            endpoints[kind] = destination
                            break
                    except OSError:
                        pass
                time.sleep(0.05)
            else:
                raise RuntimeError(
                    f"{kind} startup timed out (endpoint present={marker.exists()}, readiness HTTP={status})"
                )
        if layout == "baseline":
            platform = gateway = endpoints["baseline"]
        else:
            platform, gateway = endpoints["platform"], endpoints["gateway"]
            assert (
                platform.is_socket()
                and gateway.is_socket()
                and not platform.samefile(gateway)
            )
            assert Client(platform).request("GET", "/v1/models")[0] == 404
            assert Client(gateway).request("GET", "/api/session")[0] == 404
        if gateway_measurement is not None:
            gateway_measurement["ready_high_water_bytes"] = peak_rss()
        yield platform, gateway, private
        if gateway_measurement is not None:
            gateway_measurement["final_high_water_bytes"] = peak_rss()


def seeded(platform, private):
    admin = Client(platform)
    admin.json(
        "/api/login",
        {
            "role": "admin",
            "code": (private / "admin-access.txt").read_text().splitlines()[1],
        },
    )
    team = admin.json(
        "/api/admin/teams",
        {"number": 1, "name": "Fixture team", "project": "Fixture", "track": ""},
    )["team"]["id"]
    code = admin.json("/api/admin/team-access", {"action": "issue", "teamId": team})[
        "code"
    ]
    user = Client(platform)
    user.json("/api/login", {"role": "team", "code": code})
    revision = admin.json("/api/admin")["config"]["writeRevisions"]["submissions"]
    admin.json(
        "/api/admin/settings",
        {"submissionsOpen": True, "expectedRevisions": {"submissions": revision}},
    )
    return admin, user, team, code


if __name__ == "__main__":
    child(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
