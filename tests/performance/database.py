"""Compare SQLite connection reuse, durable writes and page-cache settings on synthetic data."""
import argparse
from contextlib import closing
import json
from math import ceil
from pathlib import Path
import platform
import random
import sqlite3
from statistics import median
import sys
from tempfile import TemporaryDirectory
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[2]
PROFILES = {
    "defaults": (),
    "normal": ("PRAGMA synchronous=NORMAL",),
    "larger-cache": ("PRAGMA cache_size=-4096",),
    "mmap": ("PRAGMA mmap_size=33554432",),
}


def run(module, path, profile, count):
    rng = random.Random(20261118)
    pool = module.ConnectionPool(path) if hasattr(module, "ConnectionPool") else None
    connect = pool.connection if pool else lambda: module.connection(path)
    with connect() as db:
        for sql in PROFILES[profile]:
            db.execute(sql)
        pragmas = {k: db.execute(f"PRAGMA {k}").fetchone()[0] for k in ("synchronous", "cache_size", "mmap_size")}
    timings = {"read": [], "write": []}
    for index in range(count + 50):
        ids = [rng.randrange(1, 16385) for _ in range(24)]
        kind = "write" if index % 4 == 0 else "read"
        t0 = perf_counter_ns()
        with connect() as db:
            if kind == "write":
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE fixture SET revision=revision+1 WHERE id=?", (ids[0],))
            else:
                db.execute("BEGIN")
                for num in ids:
                    assert db.execute("SELECT length(payload) FROM fixture WHERE id=?", (num,)).fetchone()[0] == 1024
        elapsed = (perf_counter_ns() - t0) / 1000
        if index >= 50:
            timings[kind].append(elapsed)
    if pool:
        pool.close()
    return {"pragmas": pragmas, **{kind: {"count": len(samples), "p50_us": round(median(samples), 1),
           "p95_us": round(sorted(samples)[ceil(.95*len(samples))-1], 1)}
           for kind, samples in timings.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--samples", type=int, default=600)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))
    from backend.gotc import database
    with TemporaryDirectory() as tmp:
        results = {}
        profiles = PROFILES if hasattr(database, "ConnectionPool") else ("defaults",)
        for profile in profiles:
            path = Path(tmp) / (profile+".sqlite3")
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY, payload BLOB NOT NULL, revision INTEGER NOT NULL)")
                db.executemany("INSERT INTO fixture VALUES (?,zeroblob(1024),0)", ((i,) for i in range(1,16385)))
                db.commit()
            results[profile] = run(database, path, profile, args.samples)
    print(json.dumps({"os": platform.system(), "machine": platform.machine(), "python": platform.python_version(),
                      "sqlite": sqlite3.sqlite_version, "profiles": results}, indent=2))


if __name__ == "__main__":
    main()
