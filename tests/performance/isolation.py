"""Measure legitimate login and submission latency during two admitted, local provider calls."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
from statistics import median
from threading import Barrier, Event, Thread
from time import perf_counter_ns
from local_event import Client, SLOTS, event, seeded


@contextmanager
def provider():
    entered = Barrier(SLOTS + 1, timeout=8)
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            # Signal successful admission at the actual mock provider. Leave both
            # ordinary requests open until the two platform actions have finished.
            self.rfile.read(int(self.headers["Content-Length"]))
            entered.wait()
            if not release.wait(8):
                raise TimeoutError("local measurement exceeded its bounded window")
            payload = b'{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port, entered, release
        finally:
            release.set()
            server.shutdown()
            thread.join()


def timed(barrier, operation):
    barrier.wait(timeout=5)
    start = perf_counter_ns()
    operation()
    return round((perf_counter_ns() - start) / 1_000_000, 3)


def measure(source, layout):
    samples = []
    observed_gateway = []
    for batch in range(4):
        with event(source, layout) as (platform, gateway, private), provider() as (
            port,
            entered,
            release,
        ):
            admin, team, team_id, code = seeded(platform, private)
            admin.json(
                "/api/admin/keys/upstreams",
                {
                    "upstreams": {
                        "openai": {
                            "url": f"http://127.0.0.1:{port}/v1",
                            "key": "fixture-only",
                            "models": {"fixture": [1, 2]},
                        }
                    }
                },
            )
            admin.json("/api/admin/keys/provision", {"teamId": team_id})
            key = team.json("/api/team/keys", {"name": "QA"})["key"]
            sdk = Client(gateway)

            def chat():
                response = sdk.json(
                    "/v1/chat/completions",
                    {
                        "model": "fixture",
                        "messages": [{"role": "user", "content": "ok"}],
                    },
                    headers={"Authorization": "Bearer " + key},
                )
                assert response["choices"][0]["message"]["content"] == "ok"

            version = 0
            with ThreadPoolExecutor(max_workers=4) as pool:
                for scenario in (
                    ("idle", "occupied") if batch % 2 == 0 else ("occupied", "idle")
                ):
                    active = []
                    if scenario == "occupied":
                        active = [pool.submit(chat) for _ in range(SLOTS)]
                        try:
                            entered.wait()
                            if layout == "uds":
                                status, _, body = sdk.request(
                                    "GET",
                                    "/metrics",
                                    headers={"Host": "localhost"},
                                    base="",
                                )
                                assert status == 200
                                values = dict(
                                    line.split()
                                    for line in body.decode().splitlines()
                                    if line.startswith(
                                        (
                                            "gotc_gateway_running ",
                                            "gotc_gateway_queue_depth ",
                                        )
                                    )
                                )
                                assert values == {
                                    "gotc_gateway_running": str(SLOTS),
                                    "gotc_gateway_queue_depth": "0",
                                }
                                observed_gateway.append(
                                    {k: int(v) for k, v in values.items()}
                                )
                        except Exception:
                            release.set()
                            for call in active:
                                call.result()
                            raise
                    try:
                        for i in range(7):
                            together = Barrier(2)

                            def login():
                                assert (
                                    Client(platform).json(
                                        "/api/login", {"role": "team", "code": code}
                                    )["role"]
                                    == "team"
                                )

                            def submit():
                                result = team.json(
                                    "/api/submission",
                                    {
                                        "status": "draft",
                                        "expectedVersion": version,
                                        "materials": {
                                            "summary": "Synthetic draft " + str(version)
                                        },
                                    },
                                )
                                assert result["submission"]["version"] == version + 1

                            read = pool.submit(timed, together, login)
                            write = pool.submit(timed, together, submit)
                            row = {
                                "batch": batch + 1,
                                "scenario": scenario,
                                "login_ms": read.result(),
                                "submission_ms": write.result(),
                            }
                            if i:
                                samples.append(row)
                            version += 1
                    finally:
                        release.set()
                    for call in active:
                        call.result()
                    release.clear()
    summary = {}
    for scenario in ("idle", "occupied"):
        summary[scenario] = {}
        for metric in ("login_ms", "submission_ms"):
            values = [s[metric] for s in samples if s["scenario"] == scenario]
            summary[scenario][metric] = {
                "p50": round(median(values), 3),
                "p95": sorted(values)[math.ceil(0.95 * len(values)) - 1],
            }
    summary["occupied_to_idle_p95"] = {
        metric: round(
            summary["occupied"][metric]["p95"] / summary["idle"][metric]["p95"], 3
        )
        for metric in ("login_ms", "submission_ms")
    }
    print(
        json.dumps(
            {
                "layout": layout,
                "admitted_provider_calls": SLOTS,
                "queue_waiters": 0,
                "samples_per_condition": 24,
                "observed_gateway": observed_gateway,
                "summary": summary,
                "samples": samples,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--layout", choices=["baseline", "uds"], default="uds")
    args = parser.parse_args()
    measure(args.source.resolve(), args.layout)
