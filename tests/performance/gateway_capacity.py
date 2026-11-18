"""Run exactly 80 ordinary authenticated streaming calls in a disposable local event."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import platform as host
import sqlite3
import statistics
from threading import Barrier, Event, Lock, Thread
from time import monotonic, sleep
from typing import NamedTuple
import zlib

from isolation import timed
from local_event import Client, event, seeded

TEAMS, CALLS, RUNNING, SAMPLES = 40, 80, 64, 24
ROOT = Path(__file__).resolve().parents[2]


def body(i):
    return json.dumps({'model': 'fixture', 'stream': True, 'messages': [{'role': 'user', 'content': f'Fixture {i}'}]}).encode()


def stream(i):
    delta = {'choices': [{'delta': {'content': f'Synthetic reply {i}'}}]}
    usage = {'usage': {'prompt_tokens': 11 + i, 'completion_tokens': 3 + i % 7}}
    return (b'data: ' + json.dumps(delta).encode() + b'\n\n',
            b'data: ' + json.dumps(usage).encode() + b'\n\ndata: [DONE]\n\n')


@contextmanager
def delayed_provider():
    release, lock = Event(), Lock()
    stats = {'started': 0, 'active': 0, 'peak': 0}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            assert self.path == '/v1/chat/completions'
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            i = int(request['messages'][0]['content'].removeprefix('Fixture '))
            first, last = stream(i)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(first) + len(last)))
            self.end_headers()
            self.wfile.write(first)
            self.wfile.flush()
            with lock:
                stats['active'] += 1
                stats['started'] += 1
                stats['peak'] = max(stats['peak'], stats['active'])
            try:
                if not release.wait(8):
                    raise TimeoutError('Rehearsal exceeded its local hold window')
                self.wfile.write(last)
            finally:
                with lock:
                    stats['active'] -= 1

    class LoopbackProvider(ThreadingHTTPServer):
        request_queue_size = CALLS

    with LoopbackProvider(('127.0.0.1', 0), Provider) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port, release, stats
        finally:
            release.set()
            server.shutdown()
            thread.join()


def accounts(platform, private, port):
    admin, first, first_team, code = seeded(platform, private)
    admin.json('/api/admin/keys/upstreams', {'upstreams': {'openai': {
        'url': f'http://127.0.0.1:{port}/v1', 'key': 'fixture-only', 'models': {'fixture': [1, 2]}}}})
    admin.json('/api/admin/keys/settings', {'logContent': True})
    team_ids, keys = [], []
    for number in range(1, TEAMS + 1):
        team, account = first_team, first
        if number != 1:
            team = admin.json('/api/admin/teams', {'number': number, 'name': f'Fixture {number}',
                              'project': 'Synthetic', 'track': ''})['team']['id']
            login = admin.json('/api/admin/team-access', {'action': 'issue', 'teamId': team})['code']
            account = Client(platform)
            account.json('/api/login', {'role': 'team', 'code': login})
        admin.json('/api/admin/keys/provision', {'teamId': team})
        keys.append(account.json('/api/team/keys', {'name': 'Rehearsal'})['key'])
        team_ids.append(team)
    return first, code, team_ids, keys


def probes(pool, platform, account, code, first_version, count, on_first=None):
    samples = []
    for version in range(first_version, first_version + count):
        action = on_first if version == first_version else None
        start = Barrier(2, action=action)

        def login():
            assert Client(platform).json('/api/login', {'role': 'team', 'code': code})['role'] == 'team'

        def draft():
            saved = account.json('/api/submission', {'status': 'draft', 'expectedVersion': version,
                                 'materials': {'summary': f'Synthetic draft {version}'}})
            assert saved['submission']['version'] == version + 1

        sampled_at = monotonic()
        read = pool.submit(timed, start, login)
        write = pool.submit(timed, start, draft)
        samples.append({'login_ms': read.result(), 'draft_save_ms': write.result(), 'at': sampled_at})
    return samples


def metrics(gateway):
    status, _, raw = Client(gateway).request('GET', '/metrics', headers={'Host': 'localhost'}, base='')
    assert status == 200
    names = ('gotc_gateway_running ', 'gotc_gateway_queue_depth ', 'gotc_active_streams ', 'gotc_db_lock_failures_total ')
    return {k: int(v) for k, v in (line.split() for line in raw.decode().splitlines() if line.startswith(names))}


def accounting(private, team_ids, client_ids):
    # Validate each committed request and response against the originating client's bytes, team and usage.
    with sqlite3.connect(private / 'event.sqlite3') as db:
        db.execute('ATTACH DATABASE ? AS capture', (str(private / 'gateway-content.sqlite3'),))
        rows = db.execute('''SELECT u.team_id,u.request_id,u.input_tokens,u.output_tokens,u.cost_micro,u.status,
            u.stream,u.incomplete,u.estimated,u.billing_status,c.request_bytes,c.response_bytes,c.request,c.response
            FROM api_usage u LEFT JOIN capture.content c ON c.usage_id=u.id AND c.request_id=u.request_id''').fetchall()
        captured = db.execute('SELECT count(*) FROM capture.content').fetchone()[0]
    result = Counter(usage_rows=len(rows), content_rows=captured)
    verified = set()
    for team, rid, incoming, outgoing, cost, status, streamed, incomplete, estimated, billing, reqn, respn, req, resp in rows:
        result['input_tokens'] += incoming
        result['output_tokens'] += outgoing
        result['cost_micro'] += cost
        result['request_bytes'] += reqn or 0
        result['response_bytes'] += respn or 0
        if rid not in client_ids or status != 200 or req is None or resp is None:
            continue
        i = client_ids[rid]
        upload, wire = body(i), b''.join(stream(i))
        if ((team, incoming, outgoing, cost, streamed, incomplete, estimated, billing) ==
            (team_ids[i % TEAMS], 11 + i, 3 + i % 7, (11 + i) + 2 * (3 + i % 7), 1, 0, 0, 'settled') and
            reqn == len(upload) and respn == len(wire) and zlib.decompress(req) == upload and zlib.decompress(resp) == wire):
            verified.add(i)
    result['individually_matched'] = len(verified)
    result['matching_teams'] = len({i % TEAMS for i in verified})
    return dict(result)


class StreamResult(NamedTuple):
    index: int
    status: int | str
    wire_matched: bool
    request_id: str | None
    finished_at: float


def summarize(rows):
    result = {}
    for metric in ('login_ms', 'draft_save_ms'):
        values = sorted(row[metric] for row in rows)
        result[metric] = {'p50': round(statistics.median(values), 3),
                          'p95': values[math.ceil(.95 * len(values)) - 1]}
    return result


def at_plateau(snapshot):
    return (snapshot['gotc_gateway_running'] == RUNNING and snapshot['gotc_active_streams'] == RUNNING
            and snapshot['gotc_gateway_queue_depth'] == CALLS - RUNNING)


def run(source):
    resources, samples = {}, {}
    with delayed_provider() as (port, release, provider), event(source, 'uds', capacity_measurement=resources) as (platform, gateway, private):
        account, code, team_ids, keys = accounts(platform, private, port)
        start = Barrier(CALLS + 1)

        def request(i):
            start.wait(timeout=5)
            headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + keys[i % TEAMS]}
            try:
                status, response_headers, wire = Client(gateway).request('POST', '/v1/chat/completions', body(i), headers)
            except (OSError, HTTPException) as error:
                return StreamResult(i, type(error).__name__, False, None, monotonic())
            return StreamResult(i, status, wire == b''.join(stream(i)), response_headers.get('x-client-request-id'), monotonic())

        with ThreadPoolExecutor(2) as probe_pool, ThreadPoolExecutor(CALLS) as sdk_pool:
            def measure(version, count, on_first=None):
                return probes(probe_pool, platform, account, code, version, count, on_first)

            measure(0, 2)  # Fixed untimed warm-ups before observations.
            samples['idle'] = measure(2, SAMPLES // 2)
            active = [sdk_pool.submit(request, i) for i in range(CALLS)]
            try:
                start.wait(timeout=5)
                cutoff = monotonic() + 4
                while True:
                    snapshot = metrics(gateway)
                    if (at_plateau(snapshot) and provider['active'] == RUNNING) or monotonic() >= cutoff:
                        break
                    sleep(.025)
                samples['plateau'] = measure(2 + SAMPLES // 2, SAMPLES)
                before_release = metrics(gateway)
                released_at = None

                def finish_provider_calls():
                    nonlocal released_at
                    released_at = monotonic()
                    release.set()

                drain = measure(2 + 3 * SAMPLES // 2, 4, finish_provider_calls)
            finally:
                release.set()
            calls = [future.result() for future in active]
            last_client = max(call.finished_at for call in calls)
            samples['drain'] = [row for row in drain if row['at'] <= last_client]
            samples['idle'] += measure(2 + 3 * SAMPLES // 2 + 4, SAMPLES // 2)

        final = metrics(gateway)
        verified = accounting(private, team_ids, {call.request_id: call.index for call in calls if call.request_id})

    for rows in samples.values():
        for row in rows:
            del row['at']
    load = samples['plateau'] + samples['drain']
    summary = {phase: summarize(rows) for phase, rows in dict(samples, loaded=load).items() if rows}
    ratios = {metric: summary['loaded'][metric]['p95'] / summary['idle'][metric]['p95'] for metric in summary['idle']}
    statuses = Counter(call.status for call in calls)
    matched = sum(call.wire_matched for call in calls)
    accepted = all((
        statuses == {200: CALLS}, matched == CALLS, provider['started'] == CALLS, provider['peak'] == RUNNING,
        verified['individually_matched'] == CALLS, verified['usage_rows'] == verified['content_rows'] == CALLS,
        at_plateau(snapshot), at_plateau(before_release), all(ratio <= 2 for ratio in ratios.values()),
        all(value == 0 for value in final.values()),
    ))
    return {'host': host.platform(), 'python': host.python_version(), 'calls': CALLS, 'teams': TEAMS,
            'observation_counts': {phase: len(values) for phase, values in samples.items()},
            'release_to_last_sdk_completion_ms': round(1000 * (last_client - released_at), 3),
            'plateau_before_probes': snapshot, 'plateau_after_probes': before_release,
            'after_drain': final, 'provider': dict(provider), 'http_statuses': statuses, 'matched_client_wire': matched,
            'errors': sum(n for status, n in statuses.items() if status != 200), 'accounting': verified,
            'process_resources': resources, 'fd_sampling_interval_ms': 20, 'summary_ms': summary,
            'loaded_to_idle_p95': ratios, 'samples_ms': samples, 'accepted': accepted}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT, help="Local GOTC source tree for the two service processes")
    report = run(parser.parse_args().source.resolve())
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['accepted'] else 1)
