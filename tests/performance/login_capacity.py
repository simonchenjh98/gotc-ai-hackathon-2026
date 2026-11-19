"""Forty valid event users arrive together at a disposable local platform process."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
from threading import Barrier
import time

from local_event import Client, event, seeded


def summary(samples):
    return {f'p{p}_ms': round(sorted(samples)[math.ceil(p / 100 * len(samples)) - 1], 3) for p in (50, 95)}


def timed(fn):
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000


def measure():
    source = Path(__file__).resolve().parents[2]
    with event(source, 'uds') as (platform, _gateway, private):
        admin, participant, team_id, code = seeded(platform, private)
        people = [code]
        for number in range(2, 41):
            team = admin.json('/api/admin/teams', {'number':number, 'name':f'Fixture team {number}',
                                                 'project':'Fixture', 'track':''})['team']['id']
            people.append(admin.json('/api/admin/team-access', {'action':'issue', 'teamId':team})['code'])
        together = Barrier(len(people) + 1)

        def login(number, credential):
            client = Client(platform)
            together.wait(timeout=15)
            def check():
                response = client.json('/api/login', {'role':'team', 'code':credential},
                                       headers={'X-Real-IP':f'192.0.2.{number}'})
                assert response['role'] == 'team' and 'Cookie' in client.session
            return timed(check)

        def probe():
            response = participant.json('/api/team')
            assert response['team']['id'] == team_id

        def sample(target):
            target.append(timed(probe))
            time.sleep(.004)

        idle, loaded, latencies, waves = [], [], [], []
        for _ in range(5):
            probe()
        with ThreadPoolExecutor(max_workers=len(people)) as workers:
            for _ in range(5):
                reference, occupied = [], []
                for _ in range(40):
                    sample(reference)
                jobs = [workers.submit(login, i, value) for i, value in enumerate(people, 1)]
                together.wait(timeout=15)
                while any(not job.done() for job in jobs):
                    sample(occupied)
                results = [job.result() for job in jobs]
                for _ in range(40):
                    sample(reference)
                waves.append({'valid_logins':len(results), 'login':summary(results),
                              'idle':summary(reference), 'loaded':summary(occupied)})
                idle.extend(reference)
                loaded.extend(occupied)
                latencies.extend(results)
        status, _, body = Client(platform).request('GET', '/metrics', headers={'Host':'localhost'}, base='')
        assert status == 200
        counters = {name.removeprefix('gotc_password_'):int(value) for name, value in
                    (line.split() for line in body.decode().splitlines() if line.startswith('gotc_password_'))}
    ratio = summary(loaded)['p95_ms'] / summary(idle)['p95_ms']
    return {'simultaneous_valid_logins':len(people), 'login_count':len(latencies), 'waves':waves,
            'login':summary(latencies), 'authenticated_api':'GET /api/team',
            'idle':{**summary(idle), 'n':len(idle)}, 'loaded':{**summary(loaded), 'n':len(loaded)},
            'loaded_to_idle_p95':round(ratio, 3), 'password_lane':counters,
            'accepted':len(latencies) == 5 * 40 and ratio <= 2 and counters['active'] == counters['waiting'] == 0
                       and counters['peak_active'] == 2 and 0 < counters['peak_waiting'] <= 40}


if __name__ == '__main__':
    result = measure()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['accepted'] else 1)
