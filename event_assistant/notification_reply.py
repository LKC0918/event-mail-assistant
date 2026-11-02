"""Immutable outbound-notice bindings. No authorization by title or newest task."""
import hashlib
import json
import re
import secrets
import sqlite3

from .model import SourceError, display_time

FIELDS = ('id', 'title', 'start_time', 'end_time', 'signup_start_time', 'signup_end_time')


def projection(item):
    return {name: item.get(name) for name in FIELDS}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def current_account(store):
    value = store.get_meta('registration_current_account_hash')
    if value:
        return value
    # An existing installation may predate this metadata. Never choose a latest row.
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='registrations'").fetchone():
        return ''
    hashes = {json.loads(row[0]).get('account_hash') for row in store.db.execute(
        "SELECT facts FROM registrations WHERE state IN ('pending','approved')")}
    return next(iter(hashes)) if len(hashes) == 1 and next(iter(hashes)) else ''


def prepare_binding(store, mail_key, kind, data, events, subject, now):
    enabled = kind in ('new', 'reminder', 'signup_time', 'notice_test') or data.get('reply_enabled')
    if not enabled:
        return None
    if data.get('event'):
        item = data['event']
    else:
        ids = data.get('ids', [])
        if len(ids) != 1 or ids[0] not in events:
            return None
        item = events[ids[0]]
    view = projection(item)
    if not all(view.values()) or view['signup_end_time'] <= now:
        return None
    context = data.get('reply_context')
    account = (context or {}).get('facts', {}).get('account_hash') or current_account(store)
    snapshot = {'event': view, 'account_hash': account, 'context': context}
    version = fingerprint(snapshot)
    with store.db:
        row = store.db.execute('SELECT * FROM notification_bindings WHERE mail_key=? AND view_hash=?',
                               (mail_key, version)).fetchone()
        if row:
            return dict(row)
        token = secrets.token_hex(16)
        # 邮件 notification clients can truncate a reply's Subject and discard the
        # quoted original. Put a version-specific opaque tag before the title.
        # The user just presses Reply; no code ever needs to be copied.
        for _ in range(8):
            reply_code = secrets.token_hex(3)
            if not store.db.execute('SELECT 1 FROM notification_reply_codes WHERE code=?', (reply_code,)).fetchone():
                break
        else:
            raise SourceError('notification_reply_code_collision')
        tag = '〔回信' + reply_code.upper() + '〕'
        subject = subject.replace('【活动助手】', '【活动助手】' + tag, 1) if '【活动助手】' in subject else tag + subject
        # Only collisions add a human-readable update time; no code must be copied.
        original_subject = subject
        if store.db.execute('SELECT 1 FROM notification_bindings WHERE subject=?', (subject,)).fetchone():
            subject += '｜更新于' + display_time(now)
        for _ in range(8):
            if not store.db.execute('SELECT 1 FROM notification_bindings WHERE subject=?', (subject,)).fetchone():
                break
            subject = original_subject + '｜更新于' + display_time(now) + ' ' + secrets.token_hex(2)
        else:
            raise SourceError('notification_subject_collision')
        store.db.execute('''INSERT INTO notification_bindings
          (token,mail_key,view_hash,subject,event,account_hash,context,created_at,expires_at)
          VALUES(?,?,?,?,?,?,?,?,?)''',
          (token, mail_key, version, subject, json.dumps(view, ensure_ascii=False), account,
           json.dumps(context, ensure_ascii=False) if context is not None else None,
           now, view['signup_end_time']))
        store.db.execute('INSERT INTO notification_reply_codes VALUES(?,?,?)', (reply_code, token, now))
        return dict(store.db.execute('SELECT * FROM notification_bindings WHERE token=?', (token,)).fetchone())


def received_binding(root, subject):
    path = root / 'state/monitor.sqlite3'
    if not path.is_file():
        return None
    db = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_bindings'").fetchone():
            return None
        row = db.execute('SELECT token FROM notification_bindings WHERE subject=? AND sent_at IS NOT NULL', (subject,)).fetchone()
        if row:
            return row['token']
        # Never use a similar title or the newest task. Only an issued immutable
        # reply code is a fallback; the caller still verifies the owned Sent copy.
        code = re.match(r'^(?:\[EVENTS\]\s*)?【活动助手】〔回信([0-9A-Fa-f]{6})〕', subject)
        if not code or not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_reply_codes'").fetchone():
            return None
        row = db.execute('''SELECT b.token FROM notification_reply_codes c JOIN notification_bindings b ON b.token=c.binding_token
          WHERE c.code=? AND b.sent_at IS NOT NULL''', (code[1].lower(),)).fetchone()
        return row['token'] if row else None
    except sqlite3.Error:
        raise SourceError('reply_binding_read_failed') from None
    finally:
        db.close()


def referenced_binding(root, reference, address):
    """Resolve our issued Message-ID, without guessing from a truncated title."""
    path = root / 'state/monitor.sqlite3'
    if not path.is_file():
        return None
    db = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=5)
    try:
        rows = db.execute('SELECT token,mail_key FROM notification_bindings WHERE sent_at IS NOT NULL')
        matches = [token for token, key in rows if reference == '<event-%s@%s>' %
                   (hashlib.sha256(key.encode()).hexdigest()[:40], address.split('@')[-1])]
        return matches[0] if len(matches) == 1 else None
    except sqlite3.Error:
        raise SourceError('reply_binding_read_failed') from None
    finally:
        db.close()
