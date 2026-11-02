import json
import re
import sqlite3

from .model import SourceError, unfinished


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          id TEXT PRIMARY KEY, payload TEXT NOT NULL, present INTEGER NOT NULL, observed_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS outbox(
          key TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL,
          due_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt INTEGER NOT NULL DEFAULT 0, sent_at INTEGER, last_error TEXT,
          created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, due_at, next_attempt);
        CREATE TABLE IF NOT EXISTS notification_bindings(
          token TEXT PRIMARY KEY, mail_key TEXT NOT NULL, view_hash TEXT NOT NULL,
          subject TEXT UNIQUE NOT NULL, event TEXT NOT NULL, account_hash TEXT NOT NULL,
          context TEXT, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
          sent_at INTEGER, UNIQUE(mail_key,view_hash)
        );
        CREATE TABLE IF NOT EXISTS notification_reply_codes(
          code TEXT PRIMARY KEY, binding_token TEXT UNIQUE NOT NULL, created_at INTEGER NOT NULL
        );
        """)

    def close(self):
        self.db.close()

    def get_meta(self, key, default=""):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))

    def events(self, present_only=False):
        query = "SELECT payload FROM events" + (" WHERE present=1" if present_only else "")
        return {r["id"]: r for r in (json.loads(row[0]) for row in self.db.execute(query))}

    def enqueue(self, key, kind, data, due, expires, now):
        self.db.execute("""INSERT OR IGNORE INTO outbox(key,kind,data,due_at,expires_at,created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (key, kind, json.dumps(data, ensure_ascii=False), due, expires, now))

    def apply(self, events, now, reminder_minutes=15):
        items = {l["id"]: l for l in events}
        if not items or len(items) != len(events):
            raise SourceError("invalid_full_snapshot")
        old = self.events()
        first = not self.get_meta("initialized")
        with self.db:
            self.db.execute("UPDATE events SET present=0")
            for identity, item in items.items():
                self.db.execute("""INSERT INTO events VALUES(?,?,1,?) ON CONFLICT(id) DO UPDATE SET
                                  payload=excluded.payload,present=1,observed_at=excluded.observed_at""",
                                (identity, json.dumps(item, ensure_ascii=False), now))
                if not first and identity not in old:
                    self.enqueue("new:" + identity, "new", {"ids": [identity]}, now, now + 172800, now)
                elif (not first and old.get(identity, {}).get("signup_start_time") is None
                      and item["signup_start_time"] is not None and unfinished(item, now)):
                    key = "signup_time:%s:%s" % (identity, item["signup_start_time"])
                    self.enqueue(key, "signup_time", {"ids": [identity]}, now, now + 172800, now)
            if first:
                ids = [l["id"] for l in events if unfinished(l, now)]
                self.enqueue("initial", "initial", {"ids": ids}, now, now + 86400, now)
                self.set_meta("initialized", now)
            # A completed full scan is the only point at which withdrawals are considered.
            for row in self.db.execute("SELECT key,kind,data FROM outbox WHERE status='pending'").fetchall():
                data = json.loads(row["data"])
                if row["kind"] in ("reminder", "new", "signup_time"):
                    item = items.get(data["ids"][0])
                    cancel = item is None
                    if row["kind"] == "reminder" and item is not None:
                        cancel = (item["signup_start_time"] != data["signup_start"] or
                                  not unfinished(item, now))
                    if cancel:
                        self.db.execute("UPDATE outbox SET status='cancelled' WHERE key=?", (row["key"],))
            for item in items.values():
                start = item["signup_start_time"]
                if start and start > now and unfinished(item, now):
                    key = "reminder:%s:%s" % (item["id"], start)
                    self.enqueue(key, "reminder", {"ids": [item["id"]], "signup_start": start},
                                 start - reminder_minutes * 60, start, now)
                    # A temporarily withdrawn event may return with the same start time.
                    self.db.execute("UPDATE outbox SET status='pending' WHERE key=? AND status='cancelled'", (key,))
            incident = self.get_meta("incident_start")
            if incident:
                self.db.execute("UPDATE outbox SET status='cancelled' WHERE kind='alert' AND status='pending'")
                first_success = int(self.get_meta('source_recovery_first_at', '0') or 0) or now
                successes = int(self.get_meta('source_recovery_successes', '0') or 0) + 1
                self.set_meta('source_recovery_first_at', first_success)
                self.set_meta('source_recovery_successes', successes)
                # One successful retry is a fresh snapshot, but does not prove the
                # connection has settled. Preserve the incident during flapping.
                if successes >= 3 and now - first_success >= 300:
                    sent = self.db.execute("SELECT status FROM outbox WHERE key=?", ("alert:" + incident,)).fetchone()
                    if sent and sent[0] == 'sent':
                        self.enqueue('recovery:%s:%s' % (incident, first_success), 'recovery',
                                     {'incident': incident, 'recovered_at': now,
                                      'successful_checks': successes,
                                      'failure_code': self.get_meta('source_last_failure_code')},
                                     now, now + 86400, now)
                    else:
                        self.clear_source_incident()
            self.set_meta("last_success", now)
            self.set_meta("last_error", "")
            self.set_meta("failures", 0)

    def clear_source_incident(self):
        self.set_meta('incident_start', '')
        self.set_meta('source_recovery_first_at', '')
        self.set_meta('source_recovery_successes', 0)
        self.set_meta('source_incident_failures', 0)

    def failure(self, code, now, auth=False):
        with self.db:
            code = code if re.fullmatch(r'[A-Za-z0-9_:-]{1,96}', str(code)) else 'internal_error'
            failures = int(self.get_meta("failures", "0")) + 1
            incident = self.get_meta("incident_start") or str(now)
            self.set_meta("incident_start", incident)
            self.set_meta("failures", failures)
            self.set_meta("last_error", code)
            self.set_meta('source_last_failure_code', code)
            self.set_meta('source_last_failure_at', now)
            self.set_meta('source_total_failures', int(self.get_meta('source_total_failures', '0') or 0) + 1)
            self.set_meta('source_incident_failures', int(self.get_meta('source_incident_failures', '0') or 0) + 1)
            self.set_meta('source_recovery_first_at', '')
            self.set_meta('source_recovery_successes', 0)
            self.db.execute("UPDATE outbox SET status='cancelled' WHERE kind='recovery' AND status='pending'")
            last = int(self.get_meta('last_success', '0') or 0)
            stale = now - (last or int(incident)) >= 600
            sustained = failures >= 3 and now - int(incident) >= 180
            if auth or code == 'tls_verification_failed' or sustained or stale:
                key = 'alert:' + incident
                data = {'reason': code, 'auth': auth, 'incident_start': int(incident),
                        'failed_attempts': int(self.get_meta('source_incident_failures'))}
                self.enqueue(key, 'alert', data, now, now + 604800, now)
                # A queued alert may have been cancelled by an isolated good scan.
                self.db.execute("UPDATE outbox SET status='pending',data=?,due_at=?,next_attempt=0,expires_at=? "
                                "WHERE key=? AND status IN ('cancelled','expired')",
                                (json.dumps(data, ensure_ascii=False), now, now + 604800, key))

    def due(self, now, limit=20):
        with self.db:
            self.db.execute("UPDATE outbox SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
        return self.db.execute("""SELECT * FROM outbox WHERE status='pending' AND due_at<=? AND next_attempt<=?
                                ORDER BY CASE kind WHEN 'alert' THEN 0 WHEN 'reminder' THEN 2 ELSE 1 END,
                                due_at,created_at LIMIT ?""", (now, now, limit)).fetchall()

    def was_covered(self, row, now):
        if row["kind"] != "reminder":
            return False
        data = json.loads(row["data"])
        # The introductory email can carry a near-term reminder without sending two emails.
        for sent in self.db.execute("""SELECT data FROM outbox WHERE kind IN ('initial','new','signup_time')
                                      AND status='sent' AND sent_at>=? AND sent_at<?""",
                                    (row["due_at"], data["signup_start"])):
            facts = json.loads(sent[0]).get("delivered_signup_times", {})
            if facts.get(data["ids"][0]) == data["signup_start"]:
                with self.db:
                    self.db.execute("UPDATE outbox SET status='covered' WHERE key=?", (row["key"],))
                return True
        return False

    def sent(self, row, now, delivered_signup_times=None, notification_token=None):
        data = json.loads(row["data"])
        data["delivered_signup_times"] = delivered_signup_times or {}
        with self.db:
            self.db.execute("""UPDATE outbox SET status='sent',sent_at=?,last_error=NULL,data=? WHERE key=?""",
                            (now, json.dumps(data, ensure_ascii=False), row["key"]))
            self.set_meta("last_mail_success", now)
            self.set_meta("last_mail_error", "")
            if notification_token:
                self.db.execute('UPDATE notification_bindings SET sent_at=? WHERE token=? AND mail_key=?',
                                (now, notification_token, row['key']))
            if row['kind'] == 'recovery' and data.get('incident') == self.get_meta('incident_start'):
                if not self.get_meta('last_error'):
                    self.clear_source_incident()
                else:
                    # SMTP can finish after a new failure. The next retry must
                    # track a new incident rather than suppress its alert forever.
                    self.set_meta('incident_start', self.get_meta('source_last_failure_at'))
                    self.set_meta('source_incident_failures', self.get_meta('failures', '1'))

    def mail_failure(self, row, code, now):
        attempts = row["attempts"] + 1
        delay = min(300, 30 * 2 ** min(attempts - 1, 4))
        with self.db:
            self.db.execute("UPDATE outbox SET attempts=?,next_attempt=?,last_error=? WHERE key=?",
                            (attempts, now + delay, code, row["key"]))
            self.set_meta("last_mail_error", code)

    def status(self, now):
        last = int(self.get_meta("last_success", "0"))
        pending = self.db.execute("SELECT count(*) FROM outbox WHERE status='pending' AND due_at<=?", (now,)).fetchone()[0]
        return {"last_success": last or None, "source_age_seconds": now - last if last else None,
                "source_error": self.get_meta("last_error") or None,
                "mail_error": self.get_meta("last_mail_error") or None,
                "pending_due_emails": pending,
                "present_events": self.db.execute("SELECT count(*) FROM events WHERE present=1").fetchone()[0],
                "last_mail_success": self.get_meta("last_mail_success") or None,
                'source_diagnostics': {key: self.get_meta(key) or None for key in
                    ('failures', 'incident_start', 'source_total_failures', 'source_last_failure_at',
                     'source_last_failure_code', 'source_recovery_first_at', 'source_recovery_successes')}}
