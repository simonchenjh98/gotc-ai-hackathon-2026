"""Benchmark the authenticated standings read against synthetic event data."""
import argparse
import json
import math
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def measure(service_class):
    with tempfile.TemporaryDirectory(prefix="gotc-standings-") as directory:
        event = service_class(data_dir=directory, origin="http://127.0.0.1")
        with event.db() as db:
            db.executemany(
                "INSERT INTO teams (id, number, name, updated_at) VALUES (?, ?, ?, 0)",
                [(f"team-{team}", team + 1, f"Team {team}") for team in range(128)],
            )
            db.executemany(
                "INSERT INTO accounts VALUES (?, ?, ?, X'00', X'00', 1, 0)",
                [(f"user-{n}", "judge" if n < 8 else "audience", f"User {n}") for n in range(1032)],
            )
            db.executemany(
                "INSERT INTO scores VALUES (?, ?, ?, 800, '', 'submitted', 0)",
                [(f"user-{judge}", f"team-{team}", "[16,16,16,12,12,8]")
                 for judge in range(8) for team in range(128)],
            )
            db.executemany(
                "INSERT INTO votes VALUES (?, ?, 0)",
                [(f"user-{voter+8}", f"team-{(voter+offset) % 128}")
                 for voter in range(1024) for offset in range(3)],
            )
        code = (event.data_dir / "admin-access.txt").read_text().splitlines()[1]
        _, token, _ = event.login({"role": "admin", "code": code}, "127.0.0.1")
        samples = []
        for i in range(60):
            start = time.perf_counter_ns()
            result, _, _ = event.dispatch("GET", "/api/board", {}, f"gotc_session={token}", "127.0.0.1")
            elapsed = (time.perf_counter_ns() - start) / 1_000_000
            if result["stats"] != {"teamCount": 128, "submittedScores": 1024, "audienceVotes": 3072}:
                raise ValueError("Standings fixture produced unexpected totals")
            if i >= 10:
                samples.append(elapsed)
        return {"samples": len(samples), "median_ms": round(statistics.median(samples), 3),
                "p95_ms": round(sorted(samples)[math.ceil(len(samples) * 0.95) - 1], 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path, help="Path to a prior release containing server/app.py")
    args = parser.parse_args()
    if args.legacy_root:
        sys.path.insert(0, str(args.legacy_root.resolve() / "server"))
        from app import EventService
    else:
        sys.path.insert(0, str(ROOT))
        from backend.gotc.service import EventService
    print(json.dumps(measure(EventService)))


if __name__ == "__main__":
    main()
