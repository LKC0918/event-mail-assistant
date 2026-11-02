"""Dedicated opening-time execution; never waits for inbox or SMTP work."""
import json
import time
from concurrent.futures import ThreadPoolExecutor, wait

from .config import Config
from .model import AuthNeeded, SourceError
from .registration import (Registration, SignupClient, consent_facts, fingerprint,
                           registration_view, signup_payload)
from .registration_health import error_code
from .source import Source
from .store import Store

MODE = 'dedicated_parallel_v11'
PREPARE_SECONDS = 30
PREFLIGHT_SECONDS = 15


class OpeningClient(SignupClient):
    """Three independent readers; fresh account, detail and schedule at opening."""
    def __init__(self, root, config):
        self.root = root
        super().__init__(Source(root, config))
        self.profile_reader = SignupClient(Source(root, config))
        self.detail_reader = Source(root, config)
        self.schedule_reader = SignupClient(Source(root, config))
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='signup-read')
        self.auth_version = None

    def preflight(self, identity):
        # Capture only in memory. A concurrent login/token change invalidates the
        # mixed reads rather than combining responses from different sessions.
        auth = self.source.token()
        deadline = time.monotonic() + PREFLIGHT_SECONDS
        readers = (self.profile_reader.source, self.detail_reader, self.schedule_reader.source)
        for source in readers:
            source._scan_deadline = deadline
        futures = [self.pool.submit(self.profile_reader.profile),
                   self.pool.submit(self.detail_reader.raw_detail, identity),
                   self.pool.submit(self.schedule_reader.registered)]
        try:
            profile, raw, registered = (future.result() for future in futures)
            if self.source.token() != auth:
                raise AuthNeeded('auth_changed_during_preflight')
            if str(raw.get('id')) != str(identity):
                raise SourceError('detail_identity_mismatch')
            current = registration_view(raw, profile)
            self.auth_version = auth
            return profile, current, registered
        finally:
            # Do not reuse an opener/deadline while its previous request is still
            # running. Each reader has its own opener and a bounded deadline.
            wait(futures)
            for source in readers:
                source._scan_deadline = None

    def submit(self, option, profile):
        if getattr(self, 'stopping', [False])[0]:
            raise SourceError('worker_stopping_before_post')
        if not Config.load(self.root).signup_enabled:
            raise SourceError('signup_disabled_before_post')
        if self.auth_version is None or self.source.token() != self.auth_version:
            raise AuthNeeded('auth_changed_before_post')
        return super().submit(option, profile)

    def close(self):
        self.auth_version = None
        self.pool.shutdown(wait=True)


def prepare(client, snapshot):
    """Readiness probe only. Sensitive fields and pre-opening responses are not cached."""
    profile, current, registered = client.preflight(snapshot['event_id'])
    if current['is_signup'] or any(x['id'] == current['id'] for x in registered):
        return 'already_registered'
    if fingerprint(consent_facts(current)) != snapshot['fingerprint']:
        return 'facts_changed'
    selected = snapshot['choice']
    if selected is None or selected >= len(current['options']):
        return 'option_changed'
    # Prepare/validate the payload before the opening, but discard it. The actual
    # submit uses a NEW parallel preflight and does not trust this earlier quota.
    signup_payload(current['options'][selected], profile)
    return 'ready'


def opening_candidates(tasks, now):
    return [row for row in tasks.rows() if row['state'] == 'approved' and tasks.choice_allowed(row, now)
            and 0 < json.loads(row['facts'])['signup_start_time'] - now <= PREPARE_SECONDS]


def run_submission(root, config, stopping):
    store = Store(root / 'state/monitor.sqlite3')
    tasks = Registration(store)
    client, preparer = OpeningClient(root, config), OpeningClient(root, config)
    client.stopping = stopping
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='signup-prepare')
    pending, prepared = None, set()
    next_execution, next_heartbeat = 0, 0
    try:
        while not stopping[0]:
            wall = time.time()
            enabled = Config.load(root).signup_enabled
            if wall >= next_heartbeat:
                with store.db:
                    store.set_meta('registration_executor_mode', MODE)
                    store.set_meta('registration_executor_heartbeat', int(wall))
                    store.set_meta('registration_executor_error', '')
                next_heartbeat = wall + 1
            if not enabled:
                time.sleep(0.25)
                continue
            if pending and pending[0].done():
                future, snapshot, began = pending
                code = None
                try:
                    outcome = future.result()
                except Exception as error:
                    outcome, code = 'not_ready', error_code(error)
                with store.db:
                    tasks.audit(snapshot['event_id'], 'opening_prepared', time.time(),
                                elapsed_ms=round((time.monotonic()-began)*1000), outcome=outcome, code=code)
                    current = store.db.execute('SELECT * FROM registrations WHERE token=? AND state=?', (snapshot['token'], 'approved')).fetchone()
                    if current and outcome not in ('ready', 'already_registered'):
                        tasks.notice(current, '开报前准备需要核对',
                            ['开报前检查发现账号、报名信息或联系方式暂未准备好。到点仍会核对最新信息；核验不通过不会提交。',
                             '检查结果：' + outcome + (('（' + code + '）') if code else '')],
                            int(time.time()), 'opening-preparation')
                pending = None
            # This loop never invokes mail parsing, SMTP, catalog sync or old
            # result queries. Preparation has its own pool AND its own readers.
            # Read wall time again: a preceding local operation may cross opening.
            wall = time.time()
            if wall >= next_execution:
                tasks.execute_due(client, int(wall), enabled=True, opening_guard=True)
                next_execution = tasks.next_execution_at(time.time())
            else:
                # An approval arriving after the previous timer must still wake
                # at its newly known opening, including already-due approvals.
                openings = [json.loads(r['facts'])['signup_start_time'] for r in tasks.rows()
                            if r['state'] == 'approved' and tasks.choice_allowed(r, wall)]
                next_execution = min([next_execution] + [start for start in openings if start > wall])
            if pending is None:
                for row in opening_candidates(tasks, time.time()):
                    key = (row['token'], row['fingerprint'], row['choice'])
                    if key not in prepared and json.loads(row['facts'])['signup_start_time'] - time.time() > 3:
                        prepared.add(key)
                        began = time.monotonic()
                        pending = (pool.submit(prepare, preparer, dict(row)), dict(row), began)
                        break
            delay = max(0, next_execution - time.time())
            time.sleep(min(0.25, delay) if delay > 2 else min(0.05, delay))
    except Exception as error:
        with store.db:
            store.set_meta('registration_executor_error', error_code(error))
    finally:
        pool.shutdown(wait=True)
        preparer.close()
        client.close()
        store.close()
