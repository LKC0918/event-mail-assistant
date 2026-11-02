"""Per-event consent and at-most-once submission; never cancels or checks in."""
import hashlib
import json
import re
import secrets
import time

from .model import AuthNeeded, SourceError, normalize, display_time
from .notification_reply import projection


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def number(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SourceError("invalid_registration_number")
    return value


def remaining(raw):
    total, used = number(raw.get("total_quota")), number(raw.get("signup_quota"))
    return total == 0 or used < total


def registration_view(raw, profile):
    item = normalize(raw)
    if not isinstance(raw.get("is_signup"), bool):
        raise SourceError("unknown_registration_state")
    item["is_signup"] = raw["is_signup"]
    item["open"] = raw.get("event_status") == 2
    item["has_space"] = remaining(raw)
    audience = profile.get("eligibility_code")
    account = profile.get("actor_id")
    if audience in (None, "") or not isinstance(account, str) or not account:
        raise SourceError("missing_registration_profile")
    item["account_hash"] = fingerprint(account)
    sessions = raw.get("sessions")
    if not isinstance(sessions, list):
        raise SourceError("invalid_sessions")
    options = []
    for session in sessions:
        sid = number(session.get("id"))
        mode = session.get("mode", "线上")
        if mode not in ("线上", "线下"):
            raise SourceError("invalid_session_mode")
        location = session.get("location")
        if not isinstance(location, str) or not location.strip():
            raise SourceError("missing_session_location")
        groups = session.get("groups")
        if not isinstance(groups, list):
            raise SourceError("invalid_groups")
        for group in groups:
            audiences = group.get("audiences")
            if not isinstance(audiences, list):
                raise SourceError("invalid_audiences")
            if any(d.get("eligibility_code") == audience for d in audiences):
                gid = number(group.get("id"))
                options.append({"session_id": sid, "group_id": gid, "mode": mode,
                                "location": location.strip(), "has_space": remaining(group)})
    item["options"] = sorted(options, key=lambda x: (x["session_id"], x["group_id"]))
    # Diagnostic counters change as other people sign up; never bind them into consent.
    item['quota_snapshot'] = {'event_total': number(raw.get('total_quota')),
                              'event_used': number(raw.get('signup_quota')),
                              'groups': [{ 'group_id': option['group_id'],
                                  'total': number(group['total_quota']), 'used': number(group['signup_quota'])}
                                 for option in item['options'] for session in sessions for group in session['groups']
                                 if session['id'] == option['session_id'] and group['id'] == option['group_id']]}
    return item


def consent_facts(item):
    # Quota and open/closed status may change normally; time/place/group/account may not.
    facts = {k: item[k] for k in ("id", "title", "start_time", "end_time", "signup_start_time",
                                  "signup_end_time", "account_hash")}
    facts["options"] = [{k: v for k, v in option.items() if k != "has_space"} for option in item["options"]]
    return facts


def overlaps(a, b):
    if not all(x.get(k) for x in (a, b) for k in ("start_time", "end_time")):
        raise SourceError("unknown_schedule")
    return a["start_time"] < b["end_time"] and b["start_time"] < a["end_time"]


def signup_payload(option, profile):
    actor = profile.get('actor_id')
    if not isinstance(actor, str) or not actor.strip():
        raise SourceError('missing_actor_id')
    return {'session_id': option['session_id'], 'group_id': option['group_id'], 'actor_id': actor}


class SignupClient:
    """Adapter facade; target-specific endpoints belong in an external adapter."""
    def __init__(self, source):
        self.source = source
    def profile(self):
        return self.source.profile()
    def detail(self, identity, profile):
        raw = self.source.raw_detail(identity)
        if str(raw.get('id')) != str(identity):
            raise SourceError('detail_identity_mismatch')
        return registration_view(raw, profile)
    def registered(self):
        return self.source.registered()
    def catalog(self, now, base=None):
        base = self.source.snapshot(now) if base is None else base
        profile, registered = self.profile(), self.registered()
        return [self.detail(row['id'], profile) for row in base
                if row['end_time'] and row['end_time'] > now
                and row['signup_end_time'] and row['signup_end_time'] > now], registered
    def submit(self, option, profile):
        return self.source.submit(signup_payload(option, profile))
    def result(self, group_id):
        return self.source.result(group_id)


class SubmissionClaimLost(Exception):
    pass


class Registration:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS registrations(
          event_id TEXT PRIMARY KEY, facts TEXT NOT NULL, fingerprint TEXT NOT NULL,
          token TEXT UNIQUE NOT NULL, state TEXT NOT NULL, choice INTEGER,
          conflicts TEXT NOT NULL, approved_at INTEGER, updated_at INTEGER NOT NULL,
          submitted_at INTEGER, result_checks INTEGER NOT NULL DEFAULT 0,
          next_check INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS registration_replies(
          digest TEXT PRIMARY KEY, received_at INTEGER NOT NULL, outcome TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notification_intents(
          digest TEXT PRIMARY KEY, binding_token TEXT NOT NULL, command TEXT NOT NULL,
          created_at INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
          failures INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS registration_interests(event_id TEXT PRIMARY KEY, requested_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS registration_waitlists(
          event_id TEXT PRIMARY KEY, token TEXT NOT NULL, fingerprint TEXT NOT NULL,
          choice INTEGER NOT NULL, authorized_at INTEGER NOT NULL, authorization_ref TEXT NOT NULL,
          next_check INTEGER NOT NULL, last_checked_at INTEGER, checks INTEGER NOT NULL DEFAULT 0,
          failures INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS registration_choice_groups(
          id TEXT PRIMARY KEY, authorization_ref TEXT UNIQUE NOT NULL, authorized_at INTEGER NOT NULL,
          state TEXT NOT NULL DEFAULT 'watching', selected_id TEXT, winner_id TEXT, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS registration_choice_members(
          event_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, token TEXT NOT NULL, fingerprint TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS registration_choice_deadlines(
          group_id TEXT PRIMARY KEY, stop_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
          closed_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS registration_audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, event TEXT NOT NULL,
          occurred_at REAL NOT NULL, details TEXT NOT NULL, reference TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS registration_audit_once
          ON registration_audit(event,reference) WHERE reference IS NOT NULL;
        ''')

    def rows(self):
        return self.db.execute("SELECT * FROM registrations").fetchall()

    def audit(self, identity, event, now, reference=None, **details):
        fields = {
            'preflight_started': {'delay_ms'},
            'preflight_result': {'elapsed_ms', 'event_total', 'event_used', 'group_total', 'group_used', 'open', 'has_space', 'official_is_signup'},
            'preflight_error': {'code'}, 'submission_claimed': {'group_id'},
            'submission_response_unknown': {'code'}, 'submission_sent': set(),
            'result_checked': {'result'}, 'state_changed': {'state'},
            'notice_received': set(), 'notice_decision': {'outcome'}, 'reply_decision': {'outcome'},
            'mail_transport': {'route', 'uid', 'reference_present', 'command_recognized', 'binding_found'},
            'opening_prepared': {'elapsed_ms', 'outcome', 'code'},
            'submission_attempt_started': {'delay_ms'},
        }
        if (event not in fields or set(details) - fields[event] or
                (identity is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', str(identity))) or
                (reference is not None and not re.fullmatch(r'[0-9a-f]{64}', reference))):
            raise ValueError('invalid_registration_audit')
        for value in details.values():
            if value is not None and not isinstance(value, (bool, int, float)) and not (
                    isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_:-]{1,96}', value)):
                raise ValueError('unsafe_registration_audit_value')
        self.db.execute('INSERT OR IGNORE INTO registration_audit(event_id,event,occurred_at,details,reference) VALUES(?,?,?,?,?)',
                        (str(identity) if identity is not None else None, event, now, json.dumps(details), reference))

    def record_transport(self, observation, now):
        identity = None
        if observation.get('binding_token'):
            row = self.db.execute('SELECT event FROM notification_bindings WHERE token=?', (observation['binding_token'],)).fetchone()
            identity = json.loads(row['event'])['id'] if row else None
        elif observation.get('code'):
            row = self.db.execute('SELECT event_id FROM registrations WHERE token=?', (observation['code'],)).fetchone()
            identity = row['event_id'] if row else None
        self.audit(identity, 'mail_transport', now, reference=observation['digest'],
                   route=observation['route'], uid=observation['uid'],
                   reference_present=observation['reference_present'],
                   command_recognized=observation['command_recognized'],
                   binding_found=bool(identity))

    def next_execution_at(self, now):
        openings = [json.loads(row['facts'])['signup_start_time'] for row in self.rows()
                    if row['state'] == 'approved' and self.choice_allowed(row, now)]
        return min([now + 3] + [start for start in openings if start is not None and start > now])

    def notice(self, row, title, body, now, suffix):
        facts = json.loads(row["facts"])
        self.store.enqueue("registration:%s:%s" % (row["token"], suffix), "registration",
                           {"subject": title + "｜" + facts["title"], "lines": body,
                            "event": facts}, now, now + 86400, now)

    def choice_group(self, identity):
        return self.db.execute('''SELECT g.*,m.token AS member_token,m.fingerprint AS member_fingerprint,d.stop_at
          FROM registration_choice_members m JOIN registration_choice_groups g ON g.id=m.group_id
          LEFT JOIN registration_choice_deadlines d ON d.group_id=g.id
          WHERE m.event_id=?''', (identity,)).fetchone()

    def choice_allowed(self, row, now=None):
        group = self.choice_group(row['event_id'])
        return not group or (group['state'] == 'watching' and group['selected_id'] is None and
                            (group['stop_at'] is None or (int(time.time()) if now is None else now) < group['stop_at']) and
                            group['member_token'] == row['token'] and group['member_fingerprint'] == row['fingerprint'])

    def set_choice_deadline(self, authorization_ref, stop_at, now):
        """Shorten an existing group's authorization; never rearm or cancel a signup."""
        if not isinstance(stop_at, int) or isinstance(stop_at, bool) or stop_at <= 0:
            raise ValueError('invalid_choice_deadline')
        group = self.db.execute('SELECT * FROM registration_choice_groups WHERE authorization_ref=?', (authorization_ref,)).fetchone()
        if not group:
            raise ValueError('choice_authorization_not_found')
        with self.db:
            self.db.execute('''INSERT INTO registration_choice_deadlines(group_id,stop_at,updated_at) VALUES(?,?,?)
              ON CONFLICT(group_id) DO UPDATE SET stop_at=min(stop_at,excluded.stop_at),updated_at=excluded.updated_at''',
              (group['id'], stop_at, now))
            stop_at = self.db.execute('SELECT stop_at FROM registration_choice_deadlines WHERE group_id=?', (group['id'],)).fetchone()[0]
            titles = [json.loads(x[0])['title'] for x in self.db.execute('SELECT r.facts FROM registrations r JOIN registration_choice_members m ON m.event_id=r.event_id WHERE m.group_id=? ORDER BY r.event_id', (group['id'],))]
            self.store.enqueue('choice-deadline-set:%s:%s' % (group['id'], stop_at), 'registration',
                {'subject': '二选一候补截止时间已更新', 'lines': [
                    '已将这两场的自动候补报名截止时间设为北京时间 ' + display_time(stop_at) + '。',
                    *titles, '到时停止候补，不再发起新的报名；仍然最多报名一场。',
                    '截止前已提交的结果会继续核验并通知；已经报名成功的场次保留，不会自动取消。']},
                now, max(now, stop_at) + 86400, now)
            self.expire_choice_deadlines(now)
        return stop_at

    def expire_choice_deadlines(self, now):
        """Revoke unsent work locally, even while the source is offline or backlogged."""
        for group in self.db.execute('''SELECT g.*,d.stop_at FROM registration_choice_groups g
          JOIN registration_choice_deadlines d ON d.group_id=g.id WHERE d.stop_at<=? AND d.closed_at IS NULL''', (now,)).fetchall():
            changed = self.db.execute('UPDATE registration_choice_deadlines SET closed_at=? WHERE group_id=? AND closed_at IS NULL', (now, group['id']))
            if not changed.rowcount:
                continue
            self.db.execute("UPDATE registration_choice_groups SET state='closed',updated_at=? WHERE id=? AND state IN ('watching','paused')", (now, group['id']))
            reason = '已到你设定的候补截止时间（北京时间 ' + display_time(group['stop_at']) + '），不再发起新的报名。'
            members = self.db.execute('SELECT r.* FROM registrations r JOIN registration_choice_members m ON m.event_id=r.event_id WHERE m.group_id=?', (group['id'],)).fetchall()
            for row in members:
                self.db.execute("UPDATE registrations SET state='expired',reason=?,updated_at=? WHERE event_id=? AND submitted_at IS NULL AND state IN ('pending','approved','paused','blocked')", (reason, now, row['event_id']))
                self.db.execute('UPDATE registration_waitlists SET active=0 WHERE event_id=?', (row['event_id'],))
                self.db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' AND key IN (?,?)", ('consent:' + row['token'], 'registration:' + row['token'] + ':approved'))
            if group['state'] not in ('success', 'closed'):
                self.store.enqueue('choice-deadline-stop:' + group['id'], 'registration',
                    {'subject': '二选一候补已到设定的截止时间', 'lines': [reason,
                        *[json.loads(row['facts'])['title'] for row in members],
                        '已停止两场候补；如果截止前已有提交，会继续核验结果并通知，不会重复提交或取消已有报名。']},
                    now, now + 86400, now)

    def pause_choice_group(self, group, now, reason):
        changed = self.db.execute("UPDATE registration_choice_groups SET state='paused',updated_at=? WHERE id=? AND state='watching'",
                                  (now, group['id']))
        if not changed.rowcount:
            return
        self.db.execute("UPDATE registrations SET state='paused',reason=?,updated_at=? WHERE state='approved' AND event_id IN (SELECT event_id FROM registration_choice_members WHERE group_id=?)",
                        (reason, now, group['id']))
        self.db.execute('UPDATE registration_waitlists SET active=0 WHERE event_id IN (SELECT event_id FROM registration_choice_members WHERE group_id=?)', (group['id'],))
        self.store.enqueue('choice-paused:' + group['id'], 'registration',
            {'subject': '二选一候补已暂停', 'lines': [reason, '这组不会继续自动提交；请核对最新活动信息后再处理。']}, now, now + 86400, now)

    def conflicts(self, item, registered, ignore_authorized=()):
        ignored = set(ignore_authorized)
        row = self.db.execute('SELECT * FROM registrations WHERE event_id=?', (item['id'],)).fetchone()
        if row and self.choice_allowed(row) and row['fingerprint'] == fingerprint(consent_facts(item)):
            group = self.choice_group(item['id'])
            if group:
                ignored.update(x[0] for x in self.db.execute('SELECT event_id FROM registration_choice_members WHERE group_id=?', (group['id'],)))
        # An actual official registration is never exempted as a waiting peer.
        others = {x["id"]: x for x in registered if x["id"] != item["id"]}
        for row in self.rows():
            if row["state"] in ("approved", "submitting", "uncertain", "success") and row["event_id"] != item["id"] and row['event_id'] not in ignored:
                other = json.loads(row["facts"])
                others.setdefault(other["id"], other)
        return sorted([{k: other[k] for k in ("id", "title", "start_time", "end_time")}
                       for other in others.values() if overlaps(item, other)], key=lambda x: x["id"])

    def request(self, item, conflicts, now, reason="", notify=True):
        facts, code = consent_facts(item), secrets.token_hex(16)
        group = self.choice_group(item['id'])
        if group:
            self.pause_choice_group(group, now, reason or '活动时间、地点、账号、选项或冲突变化，原二选一授权已暂停。')
        self.db.execute('UPDATE registration_waitlists SET active=0 WHERE event_id=?', (item['id'],))
        self.db.execute('''INSERT INTO registrations(event_id,facts,fingerprint,token,state,conflicts,updated_at,reason)
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET facts=excluded.facts,
          fingerprint=excluded.fingerprint,token=excluded.token,state=excluded.state,choice=NULL,
          conflicts=excluded.conflicts,approved_at=NULL,updated_at=excluded.updated_at,reason=excluded.reason''',
          (item["id"], json.dumps(facts, ensure_ascii=False), fingerprint(facts), code, "pending",
           json.dumps(conflicts, ensure_ascii=False), now, reason))
        # Any older unsent invitation for this event is no longer actionable.
        for old in self.db.execute("SELECT key,data FROM outbox WHERE kind='registration' AND status='pending'").fetchall():
            data = json.loads(old["data"])
            if data.get("invitation_id") == item["id"]:
                self.db.execute("UPDATE outbox SET status='cancelled' WHERE key=?", (old["key"],))
        if not notify:
            return
        command = "确认冲突报名" if conflicts else "确认报名"
        lines = ["请逐场决定；未回复不会报名。", reason] if reason else ["请逐场决定；未回复不会报名。"]
        if conflicts:
            lines += ["这场与以下已报名或已授权的活动冲突，当前已暂停："]
            lines += ["%s｜%s—%s" % (x["title"], display_time(x["start_time"]), display_time(x["end_time"])) for x in conflicts]
            lines += ["只有明确回复“确认冲突报名”才会继续，不会替你取消其他活动。"]
        lines += ["参加方式："]
        for index, option in enumerate(facts["options"], 1):
            lines.append("%d. %s｜%s" % (index, option["mode"], option["location"]))
        if len(facts['options']) == 1:
            suggested = command if conflicts else '报名'
        else:
            modes = [option['mode'] for option in facts['options']]
            suggested = ('确认冲突报名 ' if conflicts else '报名') + (modes[0] if modes.count(modes[0]) == 1 else '1')
        instructions = ('回复“确认冲突报名线上”或“确认冲突报名线下”；多个地点时用“确认冲突报名1”“确认冲突报名2”。'
                        if conflicts else '可以回复“报名线上”或“报名线下”；同一种方式有多个地点时，回复“报名1”“报名2”选择对应地点。')
        lines += ["直接点击回复本邮件，把以下短句放在正文第一行：", suggested, instructions,
                  "不参加请回复：不报名",
                  "无需复制确认码。回复只对应这封邮件的活动、时间、地点和方式；变化后需要重新确认。",
                  "请在报名开放前至少两分钟回复；以收到“已安排报名”回执为准。",
                  "报名授权仅适用于这封邮件对应的活动、账号和参加方式；其他平台的字段由你自己的适配器处理。"]
        self.store.enqueue("consent:" + code, "registration",
                           {"subject": "待确认报名｜" + facts["title"], "lines": lines,
                            "event": facts, "invitation_id": item["id"], 'consent_token': code,
                            'reply_enabled': True, 'reply_context': {'facts': facts, 'conflicts': conflicts}},
                           now, item["signup_end_time"], now)

    def arm_waitlist(self, item, conflicts, now, authorization_ref):
        """Trusted operator entry point after explicit consent for this exact event."""
        if (not isinstance(authorization_ref, str) or not authorization_ref.strip() or
                len(authorization_ref) > 200):
            raise ValueError('waitlist_requires_explicit_authorization')
        facts = consent_facts(item)
        if self.choice_group(item['id']):
            raise ValueError('choice_member_requires_group_authorization')
        if (len(item['options']) != 1 or conflicts or item['is_signup'] or
                not all(projection(item).values()) or not item['account_hash'] or
                now >= item['signup_end_time']):
            raise ValueError('waitlist_facts_not_eligible')
        with self.db:
            row = self.db.execute('SELECT * FROM registrations WHERE event_id=?', (item['id'],)).fetchone()
            if row and (row['state'] in ('submitting', 'uncertain', 'success') or row['submitted_at'] is not None):
                raise ValueError('waitlist_has_prior_submission')
            existing = self.waitlist(row) if row else None
            if existing and row['state'] == 'approved' and row['fingerprint'] == fingerprint(facts):
                return False
            self.request(item, conflicts, now, notify=False)
            self.db.execute("UPDATE registrations SET state='approved',choice=0,approved_at=?,reason='waiting_for_capacity' WHERE event_id=?",
                            (now, item['id']))
            row = self.db.execute('SELECT * FROM registrations WHERE event_id=?', (item['id'],)).fetchone()
            self.db.execute('''INSERT INTO registration_waitlists
              (event_id,token,fingerprint,choice,authorized_at,authorization_ref,next_check)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET
              token=excluded.token,fingerprint=excluded.fingerprint,choice=excluded.choice,
              authorized_at=excluded.authorized_at,authorization_ref=excluded.authorization_ref,
              next_check=excluded.next_check,last_checked_at=NULL,checks=0,failures=0,last_error='',active=1''',
              (item['id'], row['token'], row['fingerprint'], 0, now, authorization_ref, now))
            self.db.execute('INSERT INTO registration_interests VALUES(?,?) ON CONFLICT(event_id) DO UPDATE SET requested_at=excluded.requested_at',
                            (item['id'], now))
            self.store.enqueue('waitlist:' + row['token'], 'registration',
                {'subject': '候补监测已开启｜' + item['title'], 'event': facts,
                 'lines': ['已收到你在聊天中对这场活动的候补授权。',
                           '约每分钟核验一次名额；当前参加方式和时间未变、没有新增冲突时，恢复名额后自动提交，不再重复询问。',
                           '参加方式：' + item['options'][0]['mode'] + '｜' + item['options'][0]['location'],
                           '监测至报名截止：' + display_time(item['signup_end_time']) + '（北京时间）。',
                           '报名成功、截止或需要处理时会发邮件。未知提交结果不会重复提交；这封邮件不代表已经报名成功。',
                           '如需在提交前停止候补，直接回复本邮件“不报名”。'],
                 'reply_enabled': True, 'reply_context': {'facts': facts, 'conflicts': conflicts}},
                now, item['signup_end_time'], now)
        return True

    def waitlist(self, row):
        return self.db.execute('''SELECT * FROM registration_waitlists WHERE event_id=? AND token=?
          AND fingerprint=? AND choice=? AND active=1''',
          (row['event_id'], row['token'], row['fingerprint'], row['choice'])).fetchone()

    def arm_choice_waitlists(self, items, registered, now, authorization_ref):
        """One explicit two-event authorization, committed atomically with its latch."""
        ids = {item['id'] for item in items}
        if (len(items) != 2 or len(ids) != 2 or not overlaps(*items) or
                not isinstance(authorization_ref, str) or not authorization_ref.strip() or len(authorization_ref) > 200):
            raise ValueError('invalid_two_choice_authorization')
        accounts = {item['account_hash'] for item in items}
        registered_ids = {x['id'] for x in registered} | {x['id'] for x in items if x['is_signup']}
        winners = ids & registered_ids
        if len(accounts) != 1 or not next(iter(accounts)) or len(winners) > 1:
            raise ValueError('choice_account_or_existing_registrations_unsafe')
        with self.db:
            existing = self.db.execute('SELECT * FROM registration_choice_groups WHERE authorization_ref=?', (authorization_ref,)).fetchone()
            if existing:
                members = {x[0] for x in self.db.execute('SELECT event_id FROM registration_choice_members WHERE group_id=?', (existing['id'],))}
                if members != ids:
                    raise ValueError('choice_authorization_targets_changed')
                return existing['id']
            for item in items:
                if (len(item['options']) != 1 or not all(projection(item).values()) or now >= item['signup_end_time'] or
                        self.choice_group(item['id']) or
                        any(x['id'] not in ids for x in self.conflicts(item, registered, ids))):
                    raise ValueError('choice_facts_or_external_conflict_not_eligible')
                old = self.db.execute('SELECT * FROM registrations WHERE event_id=?', (item['id'],)).fetchone()
                if old and (old['state'] in ('submitting', 'uncertain') or old['submitted_at'] is not None) and item['id'] not in winners:
                    raise ValueError('choice_has_prior_unknown_submission')
            group_id = secrets.token_hex(16)
            # No inner commit: the daemon can never see two ungrouped approvals.
            for item in items:
                old = self.db.execute('SELECT * FROM registrations WHERE event_id=?', (item['id'],)).fetchone()
                if item['id'] not in winners or not old:
                    self.request(item, [], now, notify=False)
                row = self.db.execute('SELECT * FROM registrations WHERE event_id=?', (item['id'],)).fetchone()
                if item['id'] not in winners:
                    self.db.execute("UPDATE registrations SET state='approved',choice=0,approved_at=?,reason='waiting_for_capacity' WHERE event_id=?", (now, item['id']))
                    self.db.execute('''INSERT INTO registration_waitlists
                      (event_id,token,fingerprint,choice,authorized_at,authorization_ref,next_check)
                      VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET token=excluded.token,
                      fingerprint=excluded.fingerprint,choice=0,authorized_at=excluded.authorized_at,
                      authorization_ref=excluded.authorization_ref,next_check=excluded.next_check,
                      last_checked_at=NULL,checks=0,failures=0,last_error='',active=1''',
                      (item['id'], row['token'], row['fingerprint'], 0, now, authorization_ref, now))
                self.db.execute('INSERT INTO registration_choice_members VALUES(?,?,?,?)', (item['id'], group_id, row['token'], row['fingerprint']))
                self.db.execute('INSERT INTO registration_interests VALUES(?,?) ON CONFLICT(event_id) DO UPDATE SET requested_at=excluded.requested_at', (item['id'], now))
            self.db.execute('INSERT INTO registration_choice_groups(id,authorization_ref,authorized_at,updated_at) VALUES(?,?,?,?)', (group_id, authorization_ref, now, now))
            lines = ['已记录这两场的明确互斥候补授权：最多报名一场，无需再次确认。',
                     '约每分钟分别核验名额，先发现可用名额的一场自动尝试报名。',
                     '提交一场前先锁住整组；成功后停止另一场，结果不明时也不会提交另一场。']
            for item in items:
                lines += [item['title'], display_time(item['start_time']) + '—' + display_time(item['end_time']) + '｜' + item['options'][0]['location'],
                          '本场报名截止：' + display_time(item['signup_end_time'])]
            lines += ['报名结果会发邮件；时间、地点、账号或其他活动冲突变化会暂停旧授权。',
                      '如需停止某场，可回复那一场新版活动提醒“不报名”；不会自动取消官网已有报名。']
            self.store.enqueue('choice-enabled:' + group_id, 'registration', {'subject': '二选一候补结果：已有一场报名' if winners else '二选一候补已开启（最多报名一场）', 'lines': lines}, now, now + 86400, now)
            self.observe_choice_successes(registered + [x for x in items if x['is_signup']], now)
            return group_id

    def observe_choice_successes(self, registered, now):
        identities = {x['id'] for x in registered}
        for group in self.db.execute('SELECT * FROM registration_choice_groups').fetchall():
            winners = [x[0] for x in self.db.execute('SELECT event_id FROM registration_choice_members WHERE group_id=?', (group['id'],)) if x[0] in identities]
            if len(winners) > 1:
                self.db.execute("UPDATE registration_choice_groups SET state='uncertain',updated_at=? WHERE id=?", (now, group['id']))
                self.db.execute("UPDATE registrations SET state='blocked',reason='官网显示组内两场已有报名，已停止新的提交。',updated_at=? WHERE state IN ('pending','approved','paused') AND event_id IN (SELECT event_id FROM registration_choice_members WHERE group_id=?)", (now, group['id']))
                self.db.execute('UPDATE registration_waitlists SET active=0 WHERE event_id IN (SELECT event_id FROM registration_choice_members WHERE group_id=?)', (group['id'],))
                self.store.enqueue('choice-duplicate-official:' + group['id'], 'registration', {'subject': '二选一报名需要本人核实', 'lines': ['官网显示这两场均有报名记录，系统已停止新的提交；请本人到官网核实，不会擅自取消。']}, now, now + 86400, now)
            elif winners:
                row = self.db.execute('SELECT * FROM registrations WHERE event_id=?', (winners[0],)).fetchone()
                if row and (group['winner_id'] != winners[0] or row['state'] != 'success'):
                    self.finish(row, 'success', '官网已报名列表已确认本场报名。', now)

    def claim_submission(self, row, now):
        try:
            with self.db:
                if not self.db.in_transaction:
                    self.db.execute('BEGIN IMMEDIATE')
                now = max(now, int(time.time()))
                self.expire_choice_deadlines(now)
                expected_conflicts = {x['id'] for x in json.loads(row['conflicts'])}
                if any(x['id'] not in expected_conflicts for x in self.conflicts(json.loads(row['facts']), [])):
                    return False
                group = self.choice_group(row['event_id'])
                if group:
                    if not self.choice_allowed(row, now):
                        return False
                    claimed = self.db.execute("UPDATE registration_choice_groups SET state='locked',selected_id=?,updated_at=? WHERE id=? AND state='watching' AND selected_id IS NULL", (row['event_id'], now, group['id']))
                    if not claimed.rowcount:
                        return False
                updated = self.db.execute("UPDATE registrations SET state='submitting',submitted_at=?,next_check=?,updated_at=? WHERE token=? AND fingerprint=? AND choice=? AND state='approved'", (now, now + 3, now, row['token'], row['fingerprint'], row['choice']))
                if not updated.rowcount:
                    raise SubmissionClaimLost()
                self.db.execute('UPDATE registration_waitlists SET active=0 WHERE event_id=? AND token=?', (row['event_id'], row['token']))
                self.audit(row['event_id'], 'submission_claimed', now,
                           group_id=json.loads(row['facts'])['options'][row['choice']]['group_id'])
                if group:
                    self.db.execute("UPDATE registrations SET state='blocked',reason='二选一另一场已进入提交，已锁定本场以防重复报名。',updated_at=? WHERE state IN ('approved','pending','paused') AND event_id IN (SELECT event_id FROM registration_choice_members WHERE group_id=? AND event_id<>?)", (now, group['id'], row['event_id']))
                    self.db.execute('UPDATE registration_waitlists SET active=0 WHERE event_id IN (SELECT event_id FROM registration_choice_members WHERE group_id=?)', (group['id'],))
            return True
        except SubmissionClaimLost:
            return False

    def approved_snapshot(self, row):
        """Call inside a short write transaction, after any external reads."""
        if not self.db.in_transaction:
            self.db.execute('BEGIN IMMEDIATE')
        return self.db.execute("SELECT 1 FROM registrations WHERE event_id=? AND token=? AND fingerprint=? AND choice=? AND state='approved'",
            (row['event_id'], row['token'], row['fingerprint'], row['choice'])).fetchone() is not None

    def sync(self, items, registered, now):
        ids = {x["id"] for x in items}
        with self.db:
            self.expire_choice_deadlines(now)
            self.observe_choice_successes(registered + [x for x in items if x['is_signup']], now)
            self.store.set_meta("registration_last_scan", now)
            accounts = {item['account_hash'] for item in items}
            if len(accounts) == 1:
                self.store.set_meta('registration_current_account_hash', next(iter(accounts)))
            for row in self.rows():
                if row["event_id"] not in ids and row["state"] in ("pending", "approved"):
                    self.finish(row, "expired", "活动已撤下或报名已截止，未提交。", now)
            for item in items:
                row = self.db.execute("SELECT * FROM registrations WHERE event_id=?", (item["id"],)).fetchone()
                group = self.choice_group(item['id'])
                if group and group['state'] != 'watching':
                    continue
                if row and row["state"] in ("submitting", "uncertain", "success", "declined"):
                    continue
                if item["is_signup"]:
                    if row:
                        self.finish(row, "success", "网站显示该场活动已报名，无需再次提交。", now)
                    continue
                if not item["options"] or not all(item[k] for k in ("start_time", "end_time", "signup_start_time", "signup_end_time")):
                    if row:
                        self.finish(row, "paused", "报名选项或时间不完整，已暂停。", now)
                    continue
                conflicts = self.conflicts(item, registered)
                if not row or fingerprint(consent_facts(item)) != row["fingerprint"] or conflicts != json.loads(row["conflicts"]) or row["state"] in ("paused", "expired"):
                    interested = self.db.execute('SELECT 1 FROM registration_interests WHERE event_id=?', (item['id'],)).fetchone()
                    notify = bool(row and (row['state'] == 'approved' or interested))
                    self.request(item, conflicts, now, "报名信息或冲突情况变化，请重新确认。" if row else "", notify=notify)

    def queue_notice(self, binding_token, digest, choice, mode, conflict_accepted, reject, now):
        if not re.fullmatch(r'[0-9a-f]{32}', binding_token) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('invalid_notification_intent')
        command = dict(choice=choice, mode=mode, conflict_accepted=conflict_accepted, reject=reject)
        with self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO notification_intents(digest,binding_token,command,created_at) VALUES(?,?,?,?)',
                                       (digest, binding_token, json.dumps(command), now))
            if inserted.rowcount and not reject:
                binding = self.db.execute('SELECT event FROM notification_bindings WHERE token=? AND sent_at IS NOT NULL',
                                          (binding_token,)).fetchone()
                if binding:
                    event = json.loads(binding['event'])
                    self.audit(event['id'], 'notice_received', now, reference=digest)
                    self.store.enqueue('intent-received:' + digest, 'registration',
                        {'subject': '已收到报名回复，正在核验｜' + event['title'], 'event': event,
                         'lines': ['已收到你对这场活动的报名回复，正在核验最新报名信息。',
                         '这是收件回执，尚未确认或提交报名。',
                         '核验通过后，会再发“已安排报名”，明确告诉你将自动为你报名，无需再次回复。',
                         '多个参加方式或时间冲突会发邮件请你选择；满额、截止或无法核验也会说明。',
                         '只有收到“报名成功”才代表官网结果已确认。']}, now, now + 86400, now)

    def intent_notice(self, digest, item, title, lines, now, context=None):
        data = {'subject': title + ('｜' + item['title'] if item else ''), 'lines': lines}
        if item:
            data['event'] = projection(item)
        if context is not None:
            data.update(reply_enabled=True, reply_context=context)
        suffix = hashlib.sha256(title.encode()).hexdigest()[:12]
        self.store.enqueue('intent-result:' + digest + ':' + suffix, 'registration', data, now, now + 86400, now)

    def ask_notice_choice(self, digest, item, conflicts, now, reason=''):
        lines = [reason] if reason else []
        if conflicts:
            lines += ['这场存在时间冲突，当前没有确认或提交报名：']
            lines += [x['title'] + '｜' + display_time(x['start_time']) + '—' + display_time(x['end_time']) for x in conflicts]
        lines += ['请直接回复这封邮件选择参加方式：']
        for index, option in enumerate(item['options'], 1):
            command = ('确认冲突报名' if conflicts else '报名') + str(index)
            lines.append('%d. %s｜%s；回复：%s' % (index, option['mode'], option['location'], command))
        if not conflicts and len(item['options']) == 1:
            lines += ['只有一种方式，也可直接回复：报名这个。']
        lines += ['未收到明确选择前不会提交；不参加可回复“不报名”。', '无需复制编号或确认码，也不必寻找另一封邮件。']
        self.intent_notice(digest, item, '请确认时间冲突' if conflicts else '请选择参加方式', lines, now,
                           {'facts': consent_facts(item), 'conflicts': conflicts})

    def apply_notice(self, intent, client, now):
        started = time.monotonic()
        with self.db:
            self.expire_choice_deadlines(now)
        digest, command = intent['digest'], json.loads(intent['command'])
        previous = self.db.execute('SELECT outcome FROM registration_replies WHERE digest=?', (digest,)).fetchone()
        if previous:
            return previous['outcome']
        binding = self.db.execute('SELECT * FROM notification_bindings WHERE token=? AND sent_at IS NOT NULL',
                                  (intent['binding_token'],)).fetchone()
        if not binding:
            return 'unbound'
        sent_view = json.loads(binding['event'])
        if now >= binding['expires_at']:
            self.intent_notice(digest, sent_view, '报名已截止', ['收到回复时报名已截止，没有提交报名。'], now)
            return 'expired'
        row = self.db.execute('SELECT * FROM registrations WHERE event_id=?', (sent_view['id'],)).fetchone()
        if row and not command['reject'] and not self.choice_allowed(row, now):
            self.intent_notice(digest, sent_view, '二选一保护：此场未安排新的报名', ['同组已有提交、成功记录、授权已暂停或已到个人截止时间，不会重新开启此场或重复提交。请以本组报名结果邮件为准。'], now)
            return 'choice_group_blocked'
        if command['reject'] and row and row['state'] in ('pending', 'approved'):
            cached = json.loads(row['facts'])
            if projection(cached) == sent_view and cached['account_hash'] == binding['account_hash']:
                return self.reply(row['token'], None, False, True, digest, now)
        profile = client.profile()
        current = client.detail(sent_view['id'], profile)
        registered = client.registered()
        now += max(0, int(time.monotonic() - started))
        with self.db:
            self.expire_choice_deadlines(now)
        if row and not command['reject'] and not self.choice_allowed(row, now):
            self.intent_notice(digest, sent_view, '二选一保护：此场未安排新的报名', ['已到个人截止时间或同组状态已变化，没有安排新的报名。'], now)
            return 'choice_group_blocked'
        conflicts = self.conflicts(current, registered)
        if current['is_signup']:
            self.intent_notice(digest, current, '已报名，无需重复提交', ['官网显示你已报名，本次没有重复提交。'], now)
            return 'already_registered'
        if now >= current['signup_end_time']:
            self.intent_notice(digest, current, '报名已截止', ['官网报名已截止，没有提交报名。'], now)
            return 'expired'
        if not all(projection(current).values()) or not current['options']:
            with self.db:
                if row and row['state'] == 'approved':
                    self.db.execute("UPDATE registrations SET state='paused',choice=NULL,approved_at=NULL WHERE event_id=? AND token=? AND state='approved'", (current['id'], row['token']))
                self.intent_notice(digest, current, '报名信息暂不完整', ['当前没有完整时间或适用于本账号的参加方式，没有安排或提交报名。请等待更新后的提醒。'], now)
            return 'no_options'
        context = json.loads(binding['context']) if binding['context'] else None
        changed = (projection(current) != sent_view or not binding['account_hash'] or
                   binding['account_hash'] != current['account_hash'] or
                   (context is not None and (context['facts'] != consent_facts(current) or context['conflicts'] != conflicts)))
        with self.db:
            # Submission has its own connection/thread. Re-read inside the write
            # transaction so a slow mail preflight cannot reset a claimed POST.
            if not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT * FROM registrations WHERE event_id=?', (current['id'],)).fetchone()
            self.db.execute('INSERT INTO registration_interests VALUES(?,?) ON CONFLICT(event_id) DO UPDATE SET requested_at=excluded.requested_at',
                            (current['id'], now))
            if row and row['state'] in ('submitting', 'uncertain', 'success'):
                self.intent_notice(digest, current, '报名任务已有提交记录',
                                   ['这场已有提交或结果核验记录，没有重复提交。取消或结果不明时请查看活动平台官网“已报名”。'], now)
                return 'submission_exists'
            if not row or row['fingerprint'] != fingerprint(consent_facts(current)) or json.loads(row['conflicts']) != conflicts or row['state'] in ('paused', 'expired', 'declined'):
                self.request(current, conflicts, now, notify=False)
                row = self.db.execute('SELECT * FROM registrations WHERE event_id=?', (current['id'],)).fetchone()
            if changed:
                # Revoke any cached approval before asking about changed facts.
                self.db.execute("UPDATE registrations SET state='pending',choice=NULL,approved_at=NULL WHERE event_id=?", (current['id'],))
                self.ask_notice_choice(digest, current, conflicts, now, '原提醒与最新时间、地点、账号或冲突信息不一致，请按这封邮件重新确认。')
                return 'changed'
            if command['reject']:
                return self.reply(row['token'], None, False, True, digest, now)
            # A first reply to a basic reminder always asks before resolving
            # multiple choices or a conflict. A later answer binds the full facts.
            if context is None and (len(current['options']) != 1 or conflicts):
                self.ask_notice_choice(digest, current, conflicts, now)
                return 'question'
            choice = command['choice']
            if choice is None:
                matches = [i for i, option in enumerate(current['options'], 1) if command['mode'] is None or option['mode'] == command['mode']]
                choice = matches[0] if len(matches) == 1 else None
            if not choice or not 1 <= choice <= len(current['options']) or (conflicts and not command['conflict_accepted']):
                self.ask_notice_choice(digest, current, conflicts, now)
                return 'question'
            option = current['options'][choice - 1]
            if row['state'] == 'approved' and self.waitlist(row):
                self.intent_notice(digest, current, '候补授权已确认', ['已收到回复，本场候补授权仍有效，无需再次确认。',
                    '系统会继续等待名额；属于二选一组时，最多提交其中一场，提交结果不明时另一场也保持锁定。'], now,
                    {'facts': consent_facts(current), 'conflicts': conflicts})
                return 'already_waitlisted'
            if not current['has_space'] or not option['has_space']:
                self.intent_notice(digest, current, '当前名额已满', ['已收到报名意愿，但所选名额已满，未安排或提交报名；不会自动改选其他方式。'], now,
                                   {'facts': consent_facts(current), 'conflicts': conflicts})
                return 'full'
            if row['state'] == 'approved':
                self.intent_notice(digest, current, '已安排报名', ['本场此前已经确认，任务保留，没有重复安排。'], now)
                return 'already_approved'
        return self.reply(row['token'], choice, command['conflict_accepted'], False, digest, now)

    def process_notices(self, client, now):
        intent = self.db.execute("SELECT * FROM notification_intents WHERE state='pending' AND next_attempt<=? ORDER BY created_at LIMIT 1", (now,)).fetchone()
        if not intent:
            return
        previous = getattr(client.source, '_scan_deadline', None) if hasattr(client, 'source') else None
        if hasattr(client, 'source'):
            client.source._scan_deadline = time.monotonic() + client.source.config.scan_timeout_seconds
        try:
            outcome = self.apply_notice(intent, client, now)
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO registration_replies VALUES(?,?,?)', (intent['digest'], now, outcome))
                self.db.execute("UPDATE notification_intents SET state='done' WHERE digest=?", (intent['digest'],))
                bound = self.db.execute('SELECT event FROM notification_bindings WHERE token=?', (intent['binding_token'],)).fetchone()
                self.audit(json.loads(bound['event'])['id'] if bound else None, 'notice_decision', now,
                           reference=intent['digest'], outcome=outcome)
                self.store.set_meta('notification_preflight_error', '')
        except Exception as error:
            if not isinstance(error, (SourceError, OSError)):
                raise
            failures = intent['failures'] + 1
            with self.db:
                self.db.execute('UPDATE notification_intents SET failures=?,next_attempt=? WHERE digest=?',
                                (failures, now + min(300, 60 * 2 ** min(failures - 1, 3)), intent['digest']))
                self.store.set_meta('notification_preflight_error', str(error) if isinstance(error, SourceError) else type(error).__name__)
                if failures >= 3 and now - intent['created_at'] >= 120:
                    self.intent_notice(intent['digest'], None, '报名确认仍在核验',
                                       ['已收到你的回复，读取官网暂时失败，尚未确认或提交报名。系统会在截止前重试；完成后再通知你。'], now)
        finally:
            if hasattr(client, 'source'):
                client.source._scan_deadline = previous

    def reply(self, code, choice, conflict_accepted, reject, digest, now, mode=None):
        with self.db:
            self.expire_choice_deadlines(now)
            if self.db.execute("SELECT 1 FROM registration_replies WHERE digest=?", (digest,)).fetchone():
                return "duplicate"
            row = self.db.execute("SELECT * FROM registrations WHERE token=?", (code,)).fetchone()
            outcome = "ignored"
            if row and not reject and not self.choice_allowed(row, now):
                self.notice(row, '二选一保护：此场未安排新的报名', ['同组已有提交、成功记录、授权已暂停或已到个人截止时间，不会重新开启此场或重复提交。'], now, 'choice-blocked')
                self.db.execute('INSERT INTO registration_replies VALUES(?,?,?)', (digest, now, 'choice_group_blocked'))
                self.audit(row['event_id'], 'reply_decision', now, outcome='choice_group_blocked')
                return 'choice_group_blocked'
            if row and (row["state"] == "pending" or (reject and row["state"] == "approved")):
                facts = json.loads(row["facts"])
                if not reject and choice is None:
                    matching = [index for index, option in enumerate(facts['options'], 1)
                                if mode is None or option['mode'] == mode]
                    choice = matching[0] if len(matching) == 1 else None
                if facts["signup_end_time"] <= now:
                    self.finish(row, "expired", "回复到达时报名已截止，未提交。", now)
                    outcome = "expired"
                elif reject:
                    self.finish(row, "declined", "本场已跳过，不会提交报名。", now)
                    outcome = "declined"
                elif (choice and 1 <= choice <= len(facts["options"]) and
                      (not json.loads(row["conflicts"]) or conflict_accepted)):
                    self.db.execute("UPDATE registrations SET state='approved',choice=?,approved_at=?,updated_at=? WHERE token=?",
                                    (choice - 1, now, now, code))
                    option = facts["options"][choice - 1]
                    self.notice(row, "已安排报名", ["已收到你的确认：%s｜%s。" % (option["mode"], option["location"]),
                                "已确认参加这场活动，已安排自动报名，无需再次回复。",
                                "将在报名开放后核对最新信息并提交；这封回执不代表报名成功。"], now, "approved")
                    outcome = "approved"
                else:
                    self.notice(row, "确认未生效", ["参加方式未能唯一确定，或此场存在冲突。请用“报名1”“报名2”选择具体地点；有冲突时要明确写“确认冲突报名1”。",
                                                "当前未提交报名，请重新回复原确认邮件。"], now, "invalid-reply")
                    outcome = "invalid"
            elif row and not reject:
                # A distinct valid reply must receive feedback even after a prior
                # approval or pause; it must never reset a submission or its facts.
                suffix = 'reply-status-' + hashlib.sha256(digest.encode()).hexdigest()[:16]
                if row['state'] == 'approved':
                    waiting = bool(self.waitlist(row))
                    self.notice(row, '候补授权已确认' if waiting else '已安排报名',
                        ['已收到这次回复。' + ('本场候补仍在运行，恢复适用名额后自动尝试报名。' if waiting else '本场已确认并保留原报名安排，无需再次回复。'),
                         '这封回执不代表报名成功；最终结果会另发邮件。'], now, suffix)
                    outcome = 'already_waitlisted' if waiting else 'already_approved'
                else:
                    labels = {'paused': '已暂停，尚未提交', 'expired': '报名或候补授权已截止',
                              'submitting': '已进入提交核验，最终结果待确认', 'uncertain': '结果待核实，已停止重复提交',
                              'success': '已有官网确认成功记录', 'declined': '已撤回自动报名授权'}
                    self.notice(row, '报名任务状态回执',
                        ['已收到这次回复。本场当前状态：' + labels.get(row['state'], '需要核对最新状态') + '。',
                         '已有提交或成功记录时不会重复提交；暂停或截止的任务不会因这次旧回复重新开启，请查看最新通知和官网。'], now, suffix)
                    outcome = 'status_received'
            if row:
                self.audit(row['event_id'], 'reply_decision', now, outcome=outcome)
            self.db.execute("INSERT INTO registration_replies VALUES(?,?,?)", (digest, now, outcome))
            return outcome

    def reply_help(self, digest, reason, now):
        """Explain an unmatched reply once without changing any signup task."""
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('invalid_reply_digest')
        with self.db:
            if self.db.execute('SELECT 1 FROM registration_replies WHERE digest=?', (digest,)).fetchone():
                return 'duplicate'
            explanations = {
                'missing_reply_reference': '这封回复缺少原邮件关联信息，或主题被客户端截短。请回复新版主题前部带“回信”标记的活动邮件；不用复制该标记。',
                'replied_to_notification': '这封旧通知还没有可核验的直接回复记录，请使用最新活动提醒。',
                'sent_copy_unverified': '尚未在本邮箱的已发送记录中核实这封回复；没有批准报名。请确认使用本 邮件 邮箱发送，并保留已发送副本。',
                'missing_event_reference': '收到了一条报名短句，但没有可靠的活动关联；请直接回复具体活动提醒，不要另建只有“报名这个”作主题的新邮件。',
                'reply_reference_conflict': '这封回复的原信关联与主题标记指向不同邮件，尚未据此安排报名。请直接打开目标活动的新提醒再回复。',
                'unrecognized_or_outdated_confirmation': '回复内容或原确认邮件未能通过核验，也可能是时间、地点或参加方式已经变化。',
            }
            lines = ['已收到你的活动邮件回复，但这次确认尚未生效，尚未据此安排或提交报名。',
                     explanations.get(reason, explanations['unrecognized_or_outdated_confirmation']),
                     '请直接回复该场最新活动提醒，第一行写“报名这个”，无需寻找另一封“待确认报名”邮件。',
                     '只有一种参加方式且无冲突时会确认；有多个参加方式或时间冲突时，会再发邮件询问你。',
                     '同一种方式有多个地点时，请用“报名1”“报名2”选择；有冲突时需明确写“确认冲突报名1”。',
                     '如果再次无法识别，请在自己的邮箱客户端或网页版打开该场最新提醒并回复。',
                     '以收到“已安排报名”回执为准；收到“报名成功”才表示结果已核实。',
                     '本邮件只说明这次回复的处理结果，不会取消你此前已确认或已完成的报名。']
            self.store.enqueue('reply-help:' + digest, 'registration',
                               {'subject': '回复未生效｜报名确认操作说明', 'lines': lines},
                               now, now + 86400, now)
            self.db.execute('INSERT INTO registration_replies VALUES(?,?,?)', (digest, now, 'guidance'))
        return 'guidance'

    def finish(self, row, state, reason, now):
        self.audit(row['event_id'], 'state_changed', now, state=state)
        group = self.choice_group(row['event_id'])
        if group and state == 'success':
            self.db.execute("UPDATE registration_choice_groups SET state='success',winner_id=?,updated_at=? WHERE id=?", (row['event_id'], now, group['id']))
            self.db.execute("UPDATE registrations SET state='declined',reason='二选一中的另一场已报名成功，本场已停止候补。',updated_at=? WHERE state IN ('approved','pending','paused','blocked') AND event_id IN (SELECT event_id FROM registration_choice_members WHERE group_id=? AND event_id<>?)", (now, group['id'], row['event_id']))
            self.db.execute('UPDATE registration_waitlists SET active=0 WHERE event_id IN (SELECT event_id FROM registration_choice_members WHERE group_id=?)', (group['id'],))
            reason += ' 二选一中的另一场已停止候补，不会再自动报名。'
        elif group and state == 'uncertain':
            self.db.execute("UPDATE registration_choice_groups SET state='uncertain',updated_at=? WHERE id=? AND state<>'success'", (now, group['id']))
            reason += ' 为避免两场都报上，同组另一场仍保持锁定，不会提交。'
        elif group and state == 'paused':
            self.pause_choice_group(group, now, reason)
        self.db.execute('UPDATE registration_waitlists SET active=0 WHERE event_id=? AND token=?',
                        (row['event_id'], row['token']))
        self.db.execute("UPDATE registrations SET state=?,reason=?,updated_at=? WHERE event_id=?",
                        (state, reason, now, row["event_id"]))
        for pending in self.db.execute("SELECT key,data FROM outbox WHERE kind='registration' AND status='pending'").fetchall():
            if json.loads(pending["data"]).get("invitation_id") == row["event_id"]:
                self.db.execute("UPDATE outbox SET status='cancelled' WHERE key=?", (pending["key"],))
        self.notice(row, "报名成功" if state == "success" else "报名状态通知", [reason], now, state)
        if group and state in ('expired', 'declined'):
            remaining = self.db.execute("SELECT 1 FROM registrations WHERE state IN ('approved','pending') AND event_id IN (SELECT event_id FROM registration_choice_members WHERE group_id=?)", (group['id'],)).fetchone()
            if not remaining:
                self.db.execute("UPDATE registration_choice_groups SET state='closed',updated_at=? WHERE id=? AND state='watching'", (now, group['id']))

    def execute_due(self, client, now, enabled=False, opening_guard=False):
        with self.db:
            self.expire_choice_deadlines(max(now, int(time.time())))
        if not enabled:
            return
        # Persist submitting BEFORE the POST. A crash or timeout must never trigger a second POST.
        def priority(snapshot):
            start = json.loads(snapshot['facts'])['signup_start_time']
            return (0 if start and 0 <= now - start < 60 else 1, start or 0, snapshot['event_id'])
        for snapshot in sorted(self.rows(), key=priority):
            if opening_guard:
                now = max(now, int(time.time()))
            row = self.db.execute('SELECT * FROM registrations WHERE event_id=?', (snapshot['event_id'],)).fetchone()
            facts = json.loads(row["facts"])
            if row["state"] != "approved" or now < facts["signup_start_time"]:
                continue
            if not self.choice_allowed(row):
                continue
            if opening_guard and now - facts['signup_start_time'] >= 60:
                # Bounded preflights take at most 15s; leave a 20s guard before
                # another authorized opening instead of starting an old waitlist.
                openings = [json.loads(r['facts'])['signup_start_time'] for r in self.rows()
                            if r['state'] == 'approved' and self.choice_allowed(r, now)]
                if any(now < start <= now + 20 for start in openings):
                    continue
            waitlist = self.waitlist(row)
            if waitlist and now < waitlist['next_check'] and now < facts['signup_end_time']:
                continue
            if waitlist:
                with self.db:
                    self.db.execute('UPDATE registration_waitlists SET next_check=?,last_checked_at=?,checks=checks+1 WHERE event_id=? AND token=?',
                                    (now + 60, now, row['event_id'], row['token']))
            try:
                if now >= facts["signup_end_time"]:
                    with self.db:
                        if self.approved_snapshot(row):
                            self.finish(row, "expired", "报名时间已结束，未提交。", now)
                    continue
                started = time.monotonic()
                with self.db:
                    self.audit(row['event_id'], 'preflight_started', time.time(),
                               delay_ms=max(0, round((time.time() - facts['signup_start_time']) * 1000)))
                if hasattr(client, 'preflight'):
                    profile, current, registered = client.preflight(row['event_id'])
                else:
                    profile = client.profile()
                    current = client.detail(row["event_id"], profile)
                    registered = client.registered()
                with self.db:
                    quota = current.get('quota_snapshot', {})
                    selected_quota = next((x for x in quota.get('groups', []) if x['group_id'] == facts['options'][row['choice']]['group_id']), {})
                    self.audit(row['event_id'], 'preflight_result', time.time(),
                        elapsed_ms=round((time.monotonic() - started) * 1000), event_total=quota.get('event_total'),
                        event_used=quota.get('event_used'), group_total=selected_quota.get('total'), group_used=selected_quota.get('used'),
                        open=current['open'], has_space=current['has_space'] and current['options'][row['choice']]['has_space'] if row['choice'] < len(current['options']) else False,
                        official_is_signup=current['is_signup'])
                    self.expire_choice_deadlines(max(now, int(time.time())))
                    self.observe_choice_successes(registered + ([current] if current['is_signup'] else []), now)
                refreshed = self.db.execute('SELECT state FROM registrations WHERE token=?', (row['token'],)).fetchone()
                if not refreshed or refreshed['state'] != 'approved' or not self.choice_allowed(row):
                    continue
                if waitlist:
                    with self.db:
                        self.db.execute("UPDATE registration_waitlists SET failures=0,last_error='' WHERE event_id=? AND token=?",
                                        (row['event_id'], row['token']))
                if current["is_signup"] or any(x['id'] == row['event_id'] for x in registered):
                    with self.db:
                        if self.approved_snapshot(row):
                            self.finish(row, "success", "网站显示已报名，无需再次提交。", now)
                    continue
                conflicts = self.conflicts(current, registered)
                if fingerprint(consent_facts(current)) != row["fingerprint"] or conflicts != json.loads(row["conflicts"]):
                    with self.db:
                        if self.approved_snapshot(row):
                            self.request(current, conflicts, now, "提交前发现时间、地点、报名选项或冲突变化，请重新确认。")
                    continue
                option = current["options"][row["choice"]]
                if not current["open"]:
                    if waitlist:
                        continue
                    # Allow publication-time clock skew; stop and notify after a short grace period.
                    if now - facts["signup_start_time"] > 60:
                        with self.db:
                            if self.approved_snapshot(row):
                                self.finish(row, "paused", "网站尚未开放报名，已暂停并等待下一次完整核验。", now)
                    continue
                if not current["has_space"] or not option["has_space"]:
                    if waitlist:
                        with self.db:
                            self.db.execute("UPDATE registrations SET reason='waiting_for_capacity',updated_at=? WHERE token=? AND state='approved'", (now, row['token']))
                        continue
                    with self.db:
                        if not self.approved_snapshot(row):
                            continue
                        quota = current.get('quota_snapshot', {})
                        selected_quota = next((x for x in quota.get('groups', []) if x['group_id'] == option['group_id']), {})
                        detail = (' 官网总名额：%s/%s；适用分组：%s/%s；核验时间：%s。' %
                                  (quota.get('event_used'), quota.get('event_total'), selected_quota.get('used'), selected_quota.get('total'), display_time(int(time.time())))) if quota else ''
                        self.finish(row, "paused", "所选场次或适用分组名额已满，未提交。" + detail, now)
                    continue
                # Recheck the wall clock after network reads.
                if int(time.time()) >= facts["signup_end_time"]:
                    with self.db:
                        if self.approved_snapshot(row):
                            self.finish(row, "expired", "核验期间报名截止，未提交。", now)
                    continue
                if self.claim_submission(row, now):
                    group = self.choice_group(row['event_id'])
                    if int(time.time()) >= facts['signup_end_time']:
                        with self.db:
                            self.finish(row, 'expired', '发起报名请求前已到官网报名截止时间，未发送报名请求。', int(time.time()))
                        continue
                    if group and group['stop_at'] is not None and int(time.time()) >= group['stop_at']:
                        with self.db:
                            ended = int(time.time())
                            self.expire_choice_deadlines(ended)
                            self.finish(row, 'expired', '发起报名请求前已到个人截止时间，未发送报名请求。', ended)
                        continue
                    try:
                        with self.db:
                            self.audit(row['event_id'], 'submission_attempt_started', time.time(),
                                       delay_ms=max(0, round((time.time()-facts['signup_start_time'])*1000)))
                        client.submit(option, profile)
                        with self.db:
                            self.audit(row['event_id'], 'submission_sent', time.time())
                    except Exception:
                        # An HTTP/network error can happen after the server accepted the request.
                        with self.db:
                            self.db.execute("UPDATE registrations SET reason='submission_response_unknown' WHERE token=?", (row["token"],))
                            self.audit(row['event_id'], 'submission_response_unknown', time.time(), code='response_unknown')
            except Exception as error:
                with self.db:
                    code = str(error) if isinstance(error, SourceError) and re.fullmatch(r'[A-Za-z0-9_:-]{1,96}', str(error)) else type(error).__name__
                    self.audit(row['event_id'], 'preflight_error', time.time(), code=code)
                if waitlist and isinstance(error, (SourceError, OSError)):
                    with self.db:
                        failures = waitlist['failures'] + 1
                        code = str(error) if isinstance(error, SourceError) and re.fullmatch(r'[A-Za-z0-9_:-]{1,96}', str(error)) else type(error).__name__
                        self.db.execute('UPDATE registration_waitlists SET failures=?,last_error=?,next_check=? WHERE event_id=? AND token=?',
                                        (failures, code, now + min(300, 60 * 2 ** min(failures - 1, 3)), row['event_id'], row['token']))
                        if failures >= 3 or isinstance(error, AuthNeeded):
                            self.notice(row, '候补核验暂时受阻', ['尚未提交报名；候补授权已保留，系统会在截止前继续重试。需要重新登录时，请按登录提醒处理。'], now, 'waitlist-check-error')
                    continue
                with self.db:
                    if self.approved_snapshot(row):
                        self.finish(row, "paused", "提交前核验失败，未提交；请检查登录、账号资料或网络后重新确认。", now)
                        self.store.set_meta("registration_error", type(error).__name__)

    def reconcile(self, client, now):
        for row in self.rows():
            if row["state"] != "submitting" or row["next_check"] > now:
                continue
            facts = json.loads(row["facts"])
            result = None
            try:
                result = client.result(facts["options"][row["choice"]]["group_id"])
            except Exception:
                pass
            with self.db:
                self.audit(row['event_id'], 'result_checked', time.time(), result=result)
                if result == 2:
                    self.finish(row, "success", "活动平台报名结果接口已确认成功。请在原平台核对最终状态及后续要求。", now)
                elif row["result_checks"] >= 9:
                    self.finish(row, "uncertain", "已尝试提交，但尚未确认最终结果。为避免重复提交，已停止自动重试，请到活动平台“已报名”页面核实。", now)
                else:
                    self.db.execute("UPDATE registrations SET result_checks=result_checks+1,next_check=? WHERE token=?", (now + 3, row["token"]))
