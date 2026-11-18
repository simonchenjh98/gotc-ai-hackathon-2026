# Gateway capacity rehearsal, 18 November 2026

The candidate served **80 of 80 authenticated SSE calls from 40 synthetic teams** on macOS 27.0.1 ARM64, Python 3.14.3. The pre-fix checkpoint served 8 of 80 under the same offered burst. The virtual-time model rejected **207 of 69,266 calls (0.299%)** with the candidate's 15-per-team / 64-global running limits, 16-per-team / 64-global queue limits and 30-second queue deadline. [Candidate process results](2026-11-18-gateway-capacity.json) · [Checkpoint process results](2026-11-18-gateway-capacity-before.json) · [Replay results](2026-11-18-gateway-capacity-replay.json)

## Generated trace

Seed `20261118` generates 24 activity periods. In each, a fixed number of client lanes chooses its next offered arrival using a sampled service duration and 0–8 seconds of think time. Each period assigns extra lanes to one active team, rotating the team between periods. The busiest period starts 78 clients, including 24 from one team. The generator interpolates log-duration between the fix brief's aggregate p50, p90 and p99 values and synthetic tail endpoints of 1 and 315 seconds. The generated p50 / p90 / p99 were 15.584 / 44.378 / 149.378 seconds. The same immutable arrival list drives both replays. The raw report contains a SHA-256 digest over each big-endian binary arrival time, team number and service duration.

| Replay | Served | Rejected | Rejection breakdown | Peak running / waiting | p95 wait for queued calls eventually served |
| --- | ---: | ---: | --- | ---: | ---: |
| Checkpoint limits | 9,472 | 59,794 (86.325%) | 58,144 global queue full, 1,650 timed out | 4 / 4 | 28.340 s |
| Candidate defaults | 69,059 | 207 (0.299%) | 187 per-team queue full, 20 timed out | 64 / 20 | 27.399 s |

The checkpoint simulation gives the oldest eligible waiter the next free slot to resolve the old wake-all scheduling race deterministically. The candidate simulation serves FIFO within each team and rotates eligible teams after each grant. Synthetic lane activity, team assignments, think times and tails are assumptions. The simulation models ordinary text-call admission and its deadline. Network transport, retries, client abandonment, and host limits require separate process or environment tests. [Reproducible simulation](../../tests/performance/gateway_replay.py)

## Process rehearsal

Two fresh processes started through `python -m backend.gotc --service … --uds …` with a disposable private SQLite database and private Unix sockets. The harness created a key for each team through the organizer and contestant APIs. A barrier released exactly two small, uniquely identified chat streams per team. A provider bound to an ephemeral `127.0.0.1` port flushed one SSE event per request and withheld the final usage event and `[DONE]` marker. Gateway metrics observed 64 active streams and 16 waiting both before and after the occupied platform probes. At checkpoint `3a96a18` the metrics observed four active streams and four waiting, and 72 clients received HTTP 429.

The client issued paired valid team logins and versioned authenticated draft saves. Two warm-up pairs preceded 12 idle pairs, 24 pairs with occupied streams, up to four pairs during completion and 12 more idle pairs. The release of all provider completions coincided with the first drain pair. All four drain pairs started while model requests were outstanding. The loaded p95 combines the 24 plateau pairs and those four drain pairs, using nearest rank. The median is the mean of the two center samples for an even sample count.

| Platform operation | Idle p50 / p95 | Occupied plateau p50 / p95 | Drain p50 / p95 | All loaded p50 / p95 | Loaded / idle p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Login | 19.184 / 21.059 ms | 19.013 / 20.489 ms | 22.011 / 22.616 ms | 19.093 / 22.377 ms | 1.063× |
| Draft save | 1.163 / 1.405 ms | 1.126 / 1.237 ms | 2.461 / 2.716 ms | 1.139 / 2.644 ms | 1.882× |

The 80 clients finished within 87.957 ms of releasing the delayed provider. All HTTP responses were 200. For each client the harness matched the full SSE response and joined its request ID and team to a unique usage row and retained content row. It checked the exact uncompressed request and response bytes, byte counts, settled cost, token counts and completion flags. Totals were **7,430 request bytes, 11,590 SSE response bytes, 4,040 input tokens, 474 output tokens and 4,988 micro-USD**. The gateway drained to zero active and queued requests and reported zero SQLite lock failures. The drain duration covers receipt of the remaining provider output, 16 subsequent admissions, gateway processing, SQLite recording and delivery to clients. [Raw samples and counters](2026-11-18-gateway-capacity.json)

| Process | RSS high-water at readiness | Final RSS high-water | Peak sampled open FDs |
| --- | ---: | ---: | ---: |
| Platform | 85,262,336 B | 85,983,232 B | 16 |
| Gateway | 66,273,280 B | 72,073,216 B | 162 |

RSS uses each service's `getrusage(RUSAGE_SELF).ru_maxrss`. FD counts sample the service's descriptor directory every 20 ms and can miss shorter peaks. The client and fake provider run outside the measured processes. The run uses small JSON bodies and SSE events, one local provider, the default gateway thread limit and synthetic content logging enabled. The initial candidate run measured the plateau alone and captured both p95 ratios before the drain probes were added. Both candidate runs passed their respective checks. Shared Nginx, TLS, slow WAN links, heavyweight bodies and Linux cgroup limits require their respective rehearsals. [Initial plateau samples](2026-11-18-gateway-capacity-plateau.json) · [Process harness](../../tests/performance/gateway_capacity.py)

## Reproduction

Run from the repository root with its locked virtual environment. The process harness accepts a local source checkout and provisions its own synthetic event and network endpoints. The checkpoint invocation is expected to return exit status 1 when the candidate gate fails. Machine load and concurrent scheduling affect timing and which checkpoint requests succeed.

```sh
.runtime/venv/bin/python -B tests/performance/gateway_replay.py
baseline_dir=$(mktemp -d)
git archive 3a96a18 backend frontend | tar -x -C "$baseline_dir"
.runtime/venv/bin/python -B tests/performance/gateway_capacity.py --source "$baseline_dir"
.runtime/venv/bin/python -B tests/performance/gateway_capacity.py
```
