"""Replay a fixed-seed synthetic workload in virtual time without making network calls."""
from collections import Counter, deque
from dataclasses import dataclass
from hashlib import sha256
from heapq import heappop, heappush
import json
import math
import random
from struct import pack

SEED = 20261118
CALLS, TEAMS, ROUNDS = 69266, 40, 24
# Interpolate log duration between aggregate p50/p90/p99 plus explicit synthetic tails.
QUANTILES = ((0, 1), (.1, 4), (.5, 15.7), (.9, 44.6), (.99, 149.7), (1, 315))


def duration(rng):
    u = rng.random()
    for (lo, a), (hi, b) in zip(QUANTILES, QUANTILES[1:]):
        if u <= hi:
            return a * (b / a) ** ((u - lo) / (hi - lo))
    raise AssertionError("uniform sample outside [0, 1]")


def generate():
    """Generate offered arrivals once; admission delays never change later arrivals."""
    rng = random.Random(SEED)
    trace, start = [], 0.
    for period in range(ROUNDS):
        peak = period == ROUNDS // 2
        lanes = 78 if peak else rng.randint(18, 65)
        hot_lanes = 24 if peak else max(2, lanes // 6)
        hot_team = rng.randrange(TEAMS)
        others = [team for team in range(TEAMS) if team != hot_team]
        rng.shuffle(others)
        assignments = [hot_team] * hot_lanes + [others[i % len(others)] for i in range(lanes - hot_lanes)]
        rng.shuffle(assignments)
        ready = []
        for lane in range(lanes):
            heappush(ready, (start + rng.uniform(0, .8), lane))
        latest_end = start
        for _ in range(CALLS // ROUNDS + (period < CALLS % ROUNDS)):
            arrival, lane = heappop(ready)
            seconds = duration(rng)
            end = arrival + seconds
            trace.append((arrival, assignments[lane], seconds))
            heappush(ready, (end + rng.uniform(0, 8), lane))
            latest_end = max(latest_end, end)
        start = latest_end + rng.uniform(30, 180)
    return trace


def percentile(values, fraction):
    return round(sorted(values)[math.ceil(len(values) * fraction) - 1], 3)


def describe(trace):
    live, teams = [], Counter()
    peak_global = peak_team = 0
    durations = []
    digest = sha256()
    for arrival, team, seconds in trace:
        digest.update(pack('>dId', arrival, team, seconds))
        while live and live[0][0] <= arrival:
            _, finished = heappop(live)
            teams[finished] -= 1
        heappush(live, (arrival + seconds, team))
        teams[team] += 1
        peak_global = max(peak_global, len(live))
        peak_team = max(peak_team, teams[team])
        durations.append(seconds)
    return {'seed': SEED, 'sha256_binary_arrival_team_duration': digest.hexdigest(),
            'calls': len(trace), 'teams': len(teams), 'periods': ROUNDS,
            'peak_global': peak_global, 'peak_one_team': peak_team,
            'duration_seconds': {f'p{int(p * 100)}': percentile(durations, p) for p in (.5, .9, .99)}}


@dataclass(frozen=True)
class Limits:
    team_running: int
    global_running: int
    team_queue: int
    global_queue: int
    round_robin: bool
    timeout_seconds: int = 30


def replay(trace, limits):
    # For legacy wake-all scheduling use oldest eligible waiter as a deterministic tie-break.
    running, backlog = Counter(), {}
    teams, events = deque(), []
    results = Counter()
    waits = []
    queued = peak_queue = peak_running = active = 0

    def start(now, job):
        nonlocal active, peak_running
        at, team, length = trace[job]
        running[team] += 1
        active += 1
        peak_running = max(peak_running, active)
        heappush(events, (now + length, 0, job))
        results['served'] += 1
        if now > at:
            waits.append(now - at)

    def remove(team, job):
        nonlocal queued
        backlog[team].remove(job)
        queued -= 1
        if not backlog[team]:
            del backlog[team]
            teams.remove(team)

    def dispatch(now):
        eligible = [team for team in teams if running[team] < limits.team_running]
        if not eligible:
            return
        team = eligible[0] if limits.round_robin else min(eligible, key=lambda t: backlog[t][0])
        job = backlog[team][0]
        remove(team, job)
        if team in backlog and limits.round_robin:
            teams.remove(team)
            teams.append(team)
        start(now, job)

    def advance(until):
        nonlocal active
        while events and events[0][0] <= until:
            now, kind, job = heappop(events)
            team = trace[job][1]
            if kind == 0:
                active -= 1
                running[team] -= 1
                dispatch(now)
            elif job in backlog.get(team, ()):
                remove(team, job)
                results['queue_timeout'] += 1

    for job, (arrival, team, _) in enumerate(trace):
        advance(arrival)
        if active < limits.global_running and running[team] < limits.team_running:
            start(arrival, job)
        elif queued >= limits.global_queue:
            results['global_queue_full'] += 1
        elif len(backlog.get(team, ())) >= limits.team_queue:
            results['team_queue_full'] += 1
        else:
            if team not in backlog:
                backlog[team] = deque()
                teams.append(team)
            backlog[team].append(job)
            queued += 1
            peak_queue = max(peak_queue, queued)
            heappush(events, (arrival + limits.timeout_seconds, 1, job))
    advance(math.inf)
    assert sum(results.values()) == len(trace) and not active and not queued
    rejected = len(trace) - results['served']
    return {'limits': vars(limits), 'outcomes': dict(results), 'rejected': rejected,
            'rejected_fraction': rejected / len(trace), 'peak_running': peak_running,
            'peak_queued': peak_queue, 'waited_then_served': len(waits),
            'queued_served_wait_p95_seconds': percentile(waits, .95) if waits else None}


if __name__ == '__main__':
    trace = generate()
    workload = describe(trace)
    baseline = replay(trace, Limits(2, 4, 20, 4, False))
    # Import the service defaults so a later release cannot replay stale candidate settings.
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend.gotc.gateway.admission import GatewayCapacity
    from backend.gotc.gateway.keys import QUEUE_WAIT
    current = GatewayCapacity.from_env({})
    candidate = replay(trace, Limits(current.team_running, current.global_running, current.team_queue,
                                     current.global_queue, True, QUEUE_WAIT))
    print(json.dumps({'workload': workload, 'baseline': baseline, 'candidate': candidate}, indent=2))
    assert workload['teams'] == 40 and workload['peak_global'] == 78 and workload['peak_one_team'] == 24
    assert baseline['rejected_fraction'] > .005 and candidate['rejected_fraction'] < .005
