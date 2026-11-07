"""Private, run-bound login scheduling; never starts a scoring window."""
from datetime import datetime, timedelta, timezone
import json

from errors import APIError


class JudgeLoginSchedule:
    def __init__(self, service, clock):
        self.s, self.clock = service, clock
        self.path = service.data_dir / 'judge-login-schedule.json'

    @staticmethod
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate schedule field')
            result[key] = value
        return result

    def view(self, db, stage):
        if stage not in {'roadshow', 'finals'}:
            raise ValueError('Unknown judge login stage')
        # Read on every request so a private atomic file replacement takes effect
        # immediately, including for existing cookies and invitation tokens.
        try:
            if self.path.is_symlink():
                raise ValueError('Schedule must be a regular private file')
            try:
                with self.path.open('r', encoding='utf-8') as stream:
                    raw = stream.read(4097)
            except FileNotFoundError:
                return {'loginAvailable': True, 'loginOpensAt': None, 'message': ''}
            if len(raw) > 4096:
                raise ValueError('Schedule is too large')
            config = json.loads(raw, object_pairs_hook=self.unique_object)
            if not isinstance(config, dict) or set(config) != {'opensAt', 'roadshowRunId', 'finalsRunId'}:
                raise ValueError('Invalid schedule fields')
            if type(config['opensAt']) is not int or config['opensAt'] < 0:
                raise ValueError('Invalid opening timestamp')
            for key in ('roadshowRunId', 'finalsRunId'):
                if not isinstance(config[key], str) or not config[key] or len(config[key]) > 100 or config[key] != config[key].strip():
                    raise ValueError('Invalid schedule run')
            local = datetime.fromtimestamp(config['opensAt'] / 1000, timezone(timedelta(hours=8)))
        except (OSError, ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
            return {'loginAvailable': False, 'loginOpensAt': None,
                    'message': '评委登录时间配置异常，请联系主办方。'}
        current_run = (self.s.judging.state(db, 'roadshow')['run_id'] if stage == 'roadshow'
                       else self.s.finals.state(db)['run_id'])
        if config[stage + 'RunId'] != current_run:
            return {'loginAvailable': False, 'loginOpensAt': None,
                    'message': '本轮评委登录时间尚未配置，请联系主办方。'}
        available = self.clock() >= config['opensAt']
        return {'loginAvailable': available, 'loginOpensAt': config['opensAt'],
                'message': '' if available else '评委登录将于北京时间 ' + local.strftime('%Y-%m-%d %H:%M') + ' 开放。'}

    def require_open(self, db, stage):
        status = self.view(db, stage)
        if not status['loginAvailable']:
            raise APIError(403, status['message'], 'judge_login_not_open')
