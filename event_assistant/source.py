"""Generic adapters. Bundled adapters never contact a remote website."""
import copy
import importlib
import json
import time
from pathlib import Path

from .config import secure_json
from .model import SourceError, normalize


def sample_events(now):
    def event(identity, title, opening, start):
        return {'id': identity, 'title': title,
                'url': 'https://events.example.com/events/' + identity,
                'signup_start_time': opening, 'signup_end_time': start-60,
                'start_time': start, 'end_time': start+3600,
                'is_signup': False, 'event_status': 2,
                'total_quota': 20, 'signup_quota': 0,
                'sessions': [{'id': int(identity), 'mode': '线上', 'location': '演示会场',
                              'groups': [{'id': int(identity)*10, 'total_quota': 20, 'signup_quota': 0,
                                          'audiences': [{'eligibility_code': 'public'}]}]}]}
    return [event('1001', '演示活动A：监测与提醒', now+120, now+7200),
            event('1002', '演示活动B：邮件逐场确认', now+1800, now+14400)]


class JsonAdapter:
    """Read-only input, useful when a separate collector produces canonical JSON."""
    supports_submission = False

    def __init__(self, root, config):
        self.path = Path(root) / config.source_file

    def events(self, now):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise SourceError('source_json_unavailable') from None
        if not isinstance(data, list) or not data:
            raise SourceError('source_json_requires_complete_nonempty_snapshot')
        return data

    def detail(self, identity):
        matches = [row for row in self.events(int(time.time())) if str(row.get('id')) == str(identity)]
        if len(matches) != 1:
            raise SourceError('event_not_uniquely_found')
        return copy.deepcopy(matches[0])

    def profile(self):
        return {'actor_id': 'demo-user', 'eligibility_code': 'public'}

    def registered(self):
        return []

    def session_version(self):
        return 'readonly-json-demo-profile'

    def submit(self, payload):
        raise SourceError('readonly_adapter_does_not_submit')

    def result(self, group_id):
        return 1


class DemoAdapter(JsonAdapter):
    """Fake events and fake booking state, stored only under the chosen data root."""
    supports_submission = True

    def __init__(self, root, config):
        self.root = Path(root)
        self.path = self.root / 'state/demo-events.json'
        self.bookings = self.root / 'state/demo-bookings.json'

    @classmethod
    def initialize(cls, root, now=None):
        path = Path(root) / 'state/demo-events.json'
        if not path.exists():
            secure_json(path, sample_events(int(time.time()) if now is None else now))

    def booked(self):
        return json.loads(self.bookings.read_text()) if self.bookings.exists() else {}

    def detail(self, identity):
        raw = super().detail(identity)
        if any(str(group['id']) in self.booked() for session in raw['sessions'] for group in session['groups']):
            raw['is_signup'] = True
        return raw

    def registered(self):
        return [self.detail(row['id']) for row in self.events(int(time.time())) if self.detail(row['id'])['is_signup']]

    def session_version(self):
        return 'isolated-demo-session'

    def submit(self, payload):
        matches = [(event, session, group) for event in self.events(int(time.time()))
                   for session in event['sessions'] for group in session['groups']
                   if session['id'] == payload['session_id'] and group['id'] == payload['group_id']]
        if len(matches) != 1:
            raise SourceError('demo_option_not_found')
        event, _, group = matches[0]
        now = int(time.time())
        if not event['signup_start_time'] <= now < event['signup_end_time']:
            raise SourceError('demo_outside_signup_window')
        if group['total_quota'] and group['signup_quota'] >= group['total_quota']:
            raise SourceError('demo_capacity_full')
        data = self.booked()
        data[str(group['id'])] = {'confirmed': True}
        secure_json(self.bookings, data)

    def result(self, group_id):
        return 2 if str(group_id) in self.booked() else 1


class Source:
    """Validate the adapter boundary without assuming a particular site's API."""
    def __init__(self, root, config):
        self.root, self.config, self._scan_deadline = Path(root), config, None
        if config.adapter == 'demo':
            self.adapter = DemoAdapter(root, config)
        elif config.adapter == 'json':
            self.adapter = JsonAdapter(root, config)
        elif ':' in config.adapter:
            module, factory = config.adapter.split(':', 1)
            self.adapter = getattr(importlib.import_module(module), factory)(root, config)
        else:
            raise SourceError('unknown_adapter')

    def snapshot(self, now):
        rows = [normalize(raw) for raw in self._call('events', now)]
        if not rows or len({row['id'] for row in rows}) != len(rows):
            raise SourceError('invalid_complete_snapshot')
        return rows

    def profile(self):
        data = self._call('profile')
        if not isinstance(data, dict) or not data.get('actor_id') or not data.get('eligibility_code'):
            raise SourceError('invalid_profile')
        return data

    def raw_detail(self, identity):
        raw = self._call('detail', identity)
        if str(raw.get('id')) != str(identity):
            raise SourceError('detail_identity_mismatch')
        return raw

    def registered(self):
        return [normalize(raw) for raw in self._call('registered')]

    def _call(self, name, *args):
        # A plugin must bound every network operation itself. Pass the monotonic
        # deadline explicitly; a Python thread cannot forcibly abort plugin I/O.
        deadline = self._scan_deadline or time.monotonic() + self.config.scan_timeout_seconds
        self.adapter.deadline = deadline
        if time.monotonic() >= deadline:
            raise SourceError('adapter_deadline_exceeded')
        result = getattr(self.adapter, name)(*args)
        if time.monotonic() >= deadline:
            raise SourceError('adapter_deadline_exceeded')
        return result

    def token(self):
        # Version marker, not a credential. Real adapters should expose a stable
        # marker that changes when the authenticated user or session changes.
        return self.adapter.session_version()

    def submit(self, payload):
        if not self.config.signup_enabled:
            raise SourceError('signup_disabled')
        if self.config.adapter != 'demo' and not self.config.allow_live_submissions:
            raise SourceError('live_submission_not_explicitly_enabled')
        if not self.adapter.supports_submission:
            raise SourceError('adapter_is_readonly')
        self.adapter.deadline = time.monotonic() + self.config.request_timeout_seconds
        return self.adapter.submit(payload)

    def result(self, group_id):
        result = self._call('result', group_id)
        if type(result) is not int or result not in (1, 2):
            raise SourceError('unknown_result')
        return result
