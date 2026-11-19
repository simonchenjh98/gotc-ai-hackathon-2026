"""Measure complete SDK requests against the local synthetic provider fixture."""
import argparse
from contextlib import redirect_stdout
import json
import math
from pathlib import Path
from statistics import median
import sys
from time import perf_counter_ns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2],
                        help="GOTC checkout containing the gateway and its local test fixtures")
    root = parser.parse_args().source.resolve()
    sys.path[:0] = [str(root / "tests/backend"), str(root)]
    from test_keys import GatewayTests

    # Each fixture starts a real loopback event server, a fake upstream and an empty private database.
    fixture = GatewayTests()
    fixture.setUp()
    try:
        _, _, key = fixture.enabled_key()
        requests = {
            "models": ("/v1/models", None, "GET"),
            "chat": ("/v1/chat/completions", {"model": "cheap", "messages": []}, "POST"),
            "stream": ("/v1/chat/completions", {"model": "cheap", "stream": True}, "POST"),
        }
        times = {name: [] for name in requests}
        for sample in range(30):
            for name, (path, body, method) in requests.items():
                start = perf_counter_ns()
                status, data = fixture.call(path, body, key, method=method)
                elapsed_ms = (perf_counter_ns() - start) / 1_000_000
                if status != 200:
                    raise RuntimeError(f"{name} returned HTTP {status}")
                if name == "stream":
                    assert b"data: [DONE]" in data
                elif name == "models":
                    assert any(item["id"] == "cheap" for item in json.loads(data)["data"])
                else:
                    assert json.loads(data)["choices"][0]["message"]["content"] == "hi"
                if sample >= 5:
                    times[name].append(elapsed_ms)
        summary = {}
        for name, values in times.items():
            summary[name] = {
                "count": len(values), "median_ms": round(median(values), 3),
                "p95_ms": round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 3),
                "samples_ms": [round(v, 3) for v in values],
            }
        return summary
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    with redirect_stdout(sys.stderr):
        result = main()
    print(json.dumps(result))
