"""Read only task replies; do not mark, delete, forward or persist unrelated mail."""
import email
import datetime
import hashlib
import imaplib
import json
import re
import socket
import ssl
import time
import unicodedata
from email import policy
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from .config import mail_password
from .model import SourceError
from .notification_reply import received_binding, referenced_binding

PREFIX = "[EVENTS]"
COMMAND = re.compile(r"(确认报名|确认冲突报名)\s+([1-9][0-9]?)\s+([0-9a-f]{32})\Z")
DECLINE = re.compile(r"不报名\s+([0-9a-f]{32})\Z")
SHORT_COMMAND = re.compile(r'(确认冲突报名|确认报名|报名这个|报名)\s*(线上|线下|[1-9][0-9]?)?\Z')
REFERENCE = re.compile(r"<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9.\-]+>\Z")


def message_id(key, address):
    return "<event-%s@%s>" % (hashlib.sha256(key.encode()).hexdigest()[:40], address.split("@")[-1])


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "blockquote"):
            self.skip += 1
        if not self.skip and tag in ("br", "div", "p", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "blockquote"):
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in ("div", "p", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def owned_message(msg, address):
    if len(msg.get_all("From", [])) != 1:
        return False
    senders = getaddresses(msg.get_all("From", []))
    return (len(senders) == 1 and senders[0][1].lower() == address.lower() and
            address.lower() in [v.lower() for _, v in getaddresses(msg.get_all("To", []))] and
            msg.get("Auto-Submitted", "no") == "no")


def owned_task_message(msg, address):
    return owned_message(msg, address) and PREFIX in str(msg.get('Subject', ''))


def monitor_message(msg, address):
    if not owned_message(msg, address):
        return False
    subject = str(msg.get('Subject', '')).strip()
    for _ in range(8):
        trimmed = re.sub(r'^(?:re|回复|回覆)\s*[:：]\s*', '', subject, flags=re.I)
        if trimmed == subject:
            break
        subject = trimmed
    return (subject.startswith(('[EVENTS] 【活动助手】', '【活动助手】')) or
            SHORT_COMMAND.fullmatch(unicodedata.normalize('NFKC', subject)) is not None)


def original_subject(subject):
    subject = str(subject).strip()
    for _ in range(8):
        trimmed = re.sub(r'^(?:re|回复|回覆)\s*[:：]\s*', '', subject, flags=re.I)
        if trimmed == subject:
            break
        subject = trimmed
    return subject


def notice_command(raw, address):
    if raw is None or len(raw) > 262144:
        return None
    msg = email.message_from_bytes(raw, policy=policy.default)
    if not owned_message(msg, address):
        return None
    body = visible_body(msg)
    if body is None:
        return None
    line = unicodedata.normalize('NFKC', next((x.strip() for x in body.splitlines() if x.strip()), ''))
    reject, match = line == '不报名', SHORT_COMMAND.fullmatch(line)
    if not reject and not match:
        return None
    option = match[2] if match else None
    return {'choice': int(option) if option and option.isdigit() else None,
            'mode': option if option in ('线上', '线下') else None,
            'conflict_accepted': bool(match and match[1] == '确认冲突报名'), 'reject': reject}


def sent_header_identity(msg, address):
    if not owned_message(msg, address) or len(msg.get_all('Date', [])) != 1:
        return None
    try:
        when = parsedate_to_datetime(str(msg['Date']))
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        return (str(msg.get('Subject', '')).strip(), int(when.timestamp()))
    except (ValueError, TypeError, OverflowError):
        return None


def sent_reply_identity(raw, address):
    msg = email.message_from_bytes(raw, policy=policy.default)
    header, command = sent_header_identity(msg, address), notice_command(raw, address)
    if header is None or command is None:
        return None
    # Stable across 邮件's received/sent MIME and Message-ID rewrites. A different
    # subject, date, choice, conflict decision or decline is a different intent.
    value = {'subject': header[0], 'date': header[1], 'command': command, 'owner': address.lower()}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def unhandled_reply(raw, address):
    """Feedback only: never infer a event or authorize from an unmatched reply."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    if not monitor_message(msg, address):
        return None
    subject = str(msg.get('Subject', '')).strip()
    if SHORT_COMMAND.fullmatch(unicodedata.normalize('NFKC', original_subject(subject))):
        return {'digest': hashlib.sha256(raw).hexdigest(), 'reason': 'missing_event_reference'}
    if not re.match(r'^(?:re|回复|回覆)\s*[:：]', subject, flags=re.I):
        return None
    if not msg.get('In-Reply-To'):
        reason = 'missing_reply_reference'
    elif '待确认报名' not in subject and '回复确认测试' not in subject:
        reason = 'replied_to_notification'
    else:
        reason = 'unrecognized_or_outdated_confirmation'
    return {'digest': hashlib.sha256(raw).hexdigest(), 'reason': reason}


def visible_body(msg):
    part = msg.get_body(preferencelist=("plain", "html"))
    if not part or part.get_content_disposition() == "attachment":
        return None
    body = part.get_content()
    if part.get_content_type() == "text/html":
        parser = VisibleText()
        parser.feed(body)
        body = "".join(parser.parts)
    return body


def reply_candidate(raw, address):
    if len(raw) > 262144:
        return None
    msg = email.message_from_bytes(raw, policy=policy.default)
    if not owned_task_message(msg, address) or len(msg.get_all("In-Reply-To", [])) != 1:
        return None
    body = visible_body(msg)
    if body is None:
        return None
    # Only the first nonempty line can authorize anything. Quoted examples are ignored.
    line = next((x.strip() for x in body.splitlines() if x.strip()), "")
    match, decline = COMMAND.fullmatch(line), DECLINE.fullmatch(line)
    if not match and not decline:
        return None
    code = match[3] if match else decline[1]
    return ({"code": code, "choice": int(match[2]) if match else None,
            "conflict_accepted": bool(match and match[1] == "确认冲突报名"),
            "reject": bool(decline), "digest": hashlib.sha256(raw).hexdigest()},
            str(msg["In-Reply-To"]).strip())


def short_reply_candidate(raw, address):
    if len(raw) > 262144:
        return None
    msg = email.message_from_bytes(raw, policy=policy.default)
    if not owned_task_message(msg, address) or len(msg.get_all('In-Reply-To', [])) != 1:
        return None
    body = visible_body(msg)
    if body is None:
        return None
    line = unicodedata.normalize('NFKC', next((x.strip() for x in body.splitlines() if x.strip()), ''))
    reject = line == '不报名'
    match = SHORT_COMMAND.fullmatch(line)
    if not match and not reject:
        return None
    option = match[2] if match else None
    return ({'choice': int(option) if option and option.isdigit() else None,
             'mode': option if option in ('线上', '线下') else None,
             'conflict_accepted': bool(match and match[1] == '确认冲突报名'),
             'reject': reject, 'digest': hashlib.sha256(raw).hexdigest()},
            str(msg['In-Reply-To']).strip())


def parse_reply(raw, address, received_references=None, known_codes=()):
    candidate = reply_candidate(raw, address)
    if not candidate:
        short = short_reply_candidate(raw, address)
        if not short:
            return None
        reply, reference = short
        matches = [code for code in known_codes if
                   reference == message_id('consent:' + code, address) or
                   reference in (received_references or {}).get(code, ())]
        if len(matches) != 1:
            return None
        return dict(reply, code=matches[0])
    reply, reference = candidate
    expected = message_id("consent:" + reply["code"], address)
    verified = (received_references or {}).get(reply["code"], ())
    if reference != expected and reference not in verified:
        return None
    return reply


def received_invitation(raw, address, code, reference):
    """Bind a 邮件-rewritten ID to an issued code using the received original."""
    if raw is None or len(raw) > 262144:
        return False
    msg = email.message_from_bytes(raw, policy=policy.default)
    if (not owned_task_message(msg, address) or msg.get_all("In-Reply-To", []) or
            len(msg.get_all("Message-ID", [])) != 1 or str(msg["Message-ID"]).strip() != reference):
        return False
    subject, body = str(msg.get("Subject", "")), visible_body(msg)
    if body is None:
        return False
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return False
    invitation = subject.startswith("[EVENTS] 【活动助手】待确认报名｜") and lines[0] == "报名确认与结果"
    probe = subject.startswith("[EVENTS] 【活动助手】回复确认测试｜") and lines[0] == "收信确认测试"
    if not invitation and not probe:
        return False
    headers = msg.get_all('X-EVENTS-Consent', [])
    markers = set()
    for part in msg.walk():
        if part.get_content_type() == 'text/html' and part.get_content_disposition() != 'attachment':
            markers.update(re.findall(r'<!-- event-consent:([0-9a-f]{32}) -->', part.get_content()))
    if headers or markers:
        return (not headers or (len(headers) == 1 and str(headers[0]).strip() == code)) and (not markers or markers == {code})
    declines = {match[1] for line in lines
                if (match := DECLINE.fullmatch(line.removeprefix('不报名请回复：')))}
    commands = {match[3] for line in lines if (match := COMMAND.fullmatch(line))}
    return declines == {code} and (not invitation or commands == {code})


class MailInbox:
    def __init__(self, root, config, keepalive=False):
        self.root, self.config = root, config
        self.keepalive = keepalive
        self._client, self._signature, self._opened_at = None, None, 0
        self._reference_cache = {}
        self._notice_references = {}
        self._sent_client, self._sent_signature, self._sent_opened_at = None, None, 0
        self._sent_verified = set()

    def close(self, broken=False):
        client, self._client = self._client, None
        sent, self._sent_client = self._sent_client, None
        self._sent_signature, self._sent_opened_at = None, 0
        self._sent_verified = set()
        self._signature, self._opened_at = None, 0
        self._reference_cache = {}
        self._notice_references = {}
        for client in (client, sent):
            if client is not None:
                try:
                    # Closing the socket does not expunge or change mailbox flags.
                    if broken and hasattr(client, "shutdown"):
                        client.shutdown()
                    else:
                        client.logout()
                except Exception:
                    pass

    def verify_sent_notice(self, raw):
        identity = sent_reply_identity(raw, self.config.recipient)
        if identity is None:
            return False
        if identity in self._sent_verified:
            return True
        password = mail_password(self.config)
        if not password:
            raise SourceError('imap_configuration_invalid')
        signature = hashlib.sha256((self.config.sender + '\0' + password).encode()).digest()
        if self._sent_client and (signature != self._sent_signature or time.monotonic() - self._sent_opened_at >= 1200):
            try:
                self._sent_client.logout()
            except Exception:
                pass
            self._sent_client = None
            self._sent_verified.clear()
        if self._sent_client is None:
            self._sent_client = imaplib.IMAP4_SSL(self.config.imap_host, self.config.imap_port,
                ssl_context=ssl.create_default_context(), timeout=10)
            status, _ = self._sent_client.login(self.config.sender, password)
            if status != 'OK':
                raise SourceError('imap_login_rejected')
            self._sent_signature, self._sent_opened_at = signature, time.monotonic()
        client = self._sent_client
        if client.select(self.config.sent_folder, readonly=True)[0] != 'OK':
            raise SourceError('imap_sent_mailbox_unavailable')
        status, data = client.uid('SEARCH', None, 'FROM', '"' + self.config.sender + '"',
                                  'TO', '"' + self.config.recipient + '"')
        if status != 'OK':
            raise SourceError('imap_sent_search_failed')
        candidates = sorted({int(uid) for uid in (data[0] or b'').split()})[-150:]
        if not candidates:
            return False
        fields = '(UID BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE AUTO-SUBMITTED)])'
        status, rows = client.uid('FETCH', ','.join(map(str, candidates)), fields)
        if status != 'OK':
            raise SourceError('imap_sent_headers_failed')
        target = sent_header_identity(email.message_from_bytes(raw, policy=policy.default), self.config.recipient)
        seen, matching = set(), []
        for row in rows:
            if not isinstance(row, tuple):
                continue
            uid = re.search(rb'\bUID\s+(\d+)\b', row[0])
            if not uid or int(uid[1]) not in candidates or int(uid[1]) in seen:
                raise SourceError('imap_sent_header_uid_mismatch')
            seen.add(int(uid[1]))
            header = email.message_from_bytes(row[1], policy=policy.default)
            if sent_header_identity(header, self.config.recipient) == target:
                matching.append(uid[1].decode())
        if seen != set(candidates):
            raise SourceError('imap_sent_headers_incomplete')
        for uid in matching:
            candidate = self.fetch_message(client, uid)
            if candidate and sent_reply_identity(candidate, self.config.recipient) == identity:
                if len(self._sent_verified) >= 64:
                    self._sent_verified.clear()
                self._sent_verified.add(identity)
                return True
        return False

    def fetch_message(self, client, uid):
        status, sizes = client.uid("FETCH", str(uid), "(RFC822.SIZE)")
        if status != "OK":
            raise SourceError("imap_size_failed")
        size = re.search(rb"RFC822.SIZE (\d+)", b" ".join(x for x in sizes if isinstance(x, bytes)))
        if not size:
            raise SourceError("imap_size_missing")
        if int(size[1]) > 262144:
            return None
        status, rows = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if status != "OK":
            raise SourceError("imap_fetch_failed")
        bodies = [x[1] for x in rows if isinstance(x, tuple) and len(x) == 2]
        if not bodies:
            raise SourceError("imap_body_missing")
        return bodies[0]

    def verify_reference(self, client, code, reference):
        if len(reference) > 320 or not REFERENCE.fullmatch(reference):
            return False
        status, data = client.uid("SEARCH", None, "FROM", '"' + self.config.recipient + '"',
                                  "SUBJECT", '"[EVENTS]"', "HEADER", "Message-ID", '"' + reference + '"')
        if status != "OK":
            raise SourceError("imap_reference_search_failed")
        parents = data[0].split()
        if not parents:
            # 邮件 can return no results for Message-ID and overbroad results for BODY.
            # Inspect only IDs of at most 50 task mails, then retrieve the exact parent.
            status, data = client.uid("SEARCH", None, "FROM", '"' + self.config.recipient + '"',
                                      "SUBJECT", '"[EVENTS]"', "BODY", '"' + code + '"')
            if status != "OK":
                raise SourceError("imap_reference_search_failed")
            candidates = sorted({int(uid) for uid in data[0].split()})[-50:]
            if not candidates:
                return False
            status, rows = client.uid("FETCH", ",".join(str(uid) for uid in candidates),
                                      "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            if status != "OK":
                raise SourceError("imap_reference_headers_failed")
            parents, seen = [], set()
            for row in rows:
                if not isinstance(row, tuple) or len(row) != 2:
                    continue
                uid = re.search(rb"\bUID\s+(\d+)\b", row[0])
                if not uid:
                    raise SourceError("imap_reference_uid_missing")
                identity = int(uid[1])
                if identity not in candidates or identity in seen:
                    raise SourceError("imap_reference_uid_mismatch")
                seen.add(identity)
                headers = email.message_from_bytes(row[1], policy=policy.default)
                if len(headers.get_all("Message-ID", [])) == 1 and str(headers["Message-ID"]).strip() == reference:
                    parents.append(str(identity).encode())
            if seen != set(candidates):
                raise SourceError("imap_reference_headers_incomplete")
        if not parents or len(parents) > 2:
            return False
        return any(received_invitation(self.fetch_message(client, uid.decode()), self.config.recipient,
                                       code, reference) for uid in parents)

    def resolve_short_reference(self, client, reference, known_codes):
        if not known_codes or len(reference) > 320 or not REFERENCE.fullmatch(reference):
            return None
        status, data = client.uid('SEARCH', None, 'FROM', '"' + self.config.recipient + '"',
                                  'SUBJECT', '"[EVENTS]"', 'HEADER', '"Message-ID"', '"' + reference + '"')
        if status != 'OK':
            raise SourceError('imap_reference_search_failed')
        parents = (data[0] or b'').split()
        if not parents:
            # A short reply contains no nonce. Find only the exact received parent
            # from bounded task headers; never guess from the title or newest task.
            status, data = client.uid('SEARCH', None, 'FROM', '"' + self.config.recipient + '"',
                                      'SUBJECT', '"[EVENTS]"')
            if status != 'OK':
                raise SourceError('imap_reference_search_failed')
            candidates = sorted({int(uid) for uid in (data[0] or b'').split()})[-200:]
            if not candidates:
                return None
            status, rows = client.uid('FETCH', ','.join(map(str, candidates)),
                                      '(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            if status != 'OK':
                raise SourceError('imap_reference_headers_failed')
            seen, parents = set(), []
            for row in rows:
                if not isinstance(row, tuple):
                    continue
                uid = re.search(rb'\bUID\s+(\d+)\b', row[0])
                if not uid or int(uid[1]) not in candidates or int(uid[1]) in seen:
                    raise SourceError('imap_reference_uid_mismatch')
                seen.add(int(uid[1]))
                headers = email.message_from_bytes(row[1], policy=policy.default)
                if len(headers.get_all('Message-ID', [])) == 1 and str(headers['Message-ID']).strip() == reference:
                    parents.append(uid[1])
            if seen != set(candidates):
                raise SourceError('imap_reference_headers_incomplete')
        if not parents or len(parents) > 2:
            return None
        matches = set()
        for parent in parents:
            raw = self.fetch_message(client, parent.decode())
            if raw is None:
                continue
            for code in known_codes:
                if received_invitation(raw, self.config.recipient, code, reference):
                    matches.add(code)
        return next(iter(matches)) if len(matches) == 1 else None

    def resolve_notice_reference(self, client, msg):
        if len(msg.get_all('In-Reply-To', [])) != 1:
            return None
        reference = str(msg['In-Reply-To']).strip()
        if len(reference) > 320 or not REFERENCE.fullmatch(reference):
            return None
        direct = referenced_binding(self.root, reference, self.config.recipient)
        if direct:
            return direct
        if reference in self._notice_references:
            return self._notice_references[reference]
        status, data = client.uid('SEARCH', None, 'FROM', '"' + self.config.recipient + '"',
                                 'HEADER', '"Message-ID"', '"' + reference + '"')
        if status != 'OK':
            raise SourceError('imap_notice_reference_search_failed')
        parents = (data[0] or b'').split()
        if not parents:
            status, data = client.uid('SEARCH', None, 'FROM', '"' + self.config.recipient + '"', 'TO', '"' + self.config.recipient + '"')
            if status != 'OK':
                raise SourceError('imap_notice_reference_search_failed')
            candidates = sorted({int(uid) for uid in (data[0] or b'').split()})[-200:]
            if not candidates:
                return None
            status, rows = client.uid('FETCH', ','.join(map(str, candidates)), '(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            if status != 'OK':
                raise SourceError('imap_notice_reference_headers_failed')
            seen, parents = set(), []
            for row in rows:
                if not isinstance(row, tuple):
                    continue
                uid = re.search(rb'\bUID\s+(\d+)\b', row[0])
                if not uid or int(uid[1]) not in candidates or int(uid[1]) in seen:
                    raise SourceError('imap_notice_reference_uid_mismatch')
                seen.add(int(uid[1]))
                headers = email.message_from_bytes(row[1], policy=policy.default)
                if len(headers.get_all('Message-ID', [])) == 1 and str(headers['Message-ID']).strip() == reference:
                    parents.append(uid[1])
            if seen != set(candidates):
                raise SourceError('imap_notice_reference_headers_incomplete')
        if not parents or len(parents) > 2:
            return None
        matches = set()
        for uid in parents:
            raw = self.fetch_message(client, uid.decode())
            if raw is None:
                continue
            parent = email.message_from_bytes(raw, policy=policy.default)
            if (not monitor_message(parent, self.config.recipient) or parent.get('In-Reply-To') or
                    len(parent.get_all('Message-ID', [])) != 1 or str(parent['Message-ID']).strip() != reference):
                continue
            binding = received_binding(self.root, str(parent.get('Subject', '')).strip())
            if binding:
                markers = set()
                for part in parent.walk():
                    if part.get_content_type() == 'text/html' and part.get_content_disposition() != 'attachment':
                        markers.update(re.findall(r'<!-- event-notice:([0-9a-f]{32}) -->', part.get_content()))
                if not markers or markers == {binding}:
                    matches.add(binding)
        result = next(iter(matches)) if len(matches) == 1 else None
        if result:
            if len(self._notice_references) >= 200:
                self._notice_references.clear()
            self._notice_references[reference] = result
        return result

    def poll(self, since_uid=0, expected_validity=None, known_codes=()):
        stage = "configuration"
        try:
            password = mail_password(self.config)
            if not password:
                raise SourceError("imap_configuration_invalid")
            signature = hashlib.sha256((self.config.sender + "\0" + password).encode()).digest()
            if self._client and (signature != self._signature or time.monotonic() - self._opened_at >= 1200):
                self.close()
            if self._client is None:
                stage = "connect"
                self._client = imaplib.IMAP4_SSL(self.config.imap_host, self.config.imap_port,
                                               ssl_context=ssl.create_default_context(), timeout=10)
                stage = "login"
                status, _ = self._client.login(self.config.sender, password)
                if status != "OK":
                    raise SourceError("imap_login_rejected")
                self._signature, self._opened_at = signature, time.monotonic()
            client = self._client
            stage = "select"
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise SourceError("imap_inbox_unavailable")
            response = client.response("UIDVALIDITY")[1]
            if not response or not isinstance(response[0], bytes):
                raise SourceError("imap_uidvalidity_missing")
            validity = response[0].decode("ascii")
            if not validity.isdigit():
                raise SourceError("imap_uidvalidity_missing")
            if validity != expected_validity:
                since_uid = 0
            # Include replies to old reminders without [EVENTS]. Inspect only
            # own-to-self headers, then fetch bodies only for our monitor mail.
            stage = "search"
            status, data = client.uid("SEARCH", None, "UID", str(since_uid + 1) + ":*",
                                      "FROM", '"' + self.config.recipient + '"',
                                      "TO", '"' + self.config.recipient + '"')
            if status != "OK":
                raise SourceError("imap_search_failed")
            uids = sorted(int(x) for x in data[0].split() if int(x) > since_uid)
            replies, notices, unhandled, observations, last, verified_references = [], [], [], [], since_uid, self._reference_cache
            known_codes = set(known_codes)
            for code in list(verified_references):
                if code not in known_codes:
                    verified_references.pop(code)
            for uid in uids[:50]:
                stage = "fetch"
                status, headers = client.uid('FETCH', str(uid),
                    '(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT AUTO-SUBMITTED)])')
                header = next((part[1] for part in headers if isinstance(part, tuple)), None)
                if status != 'OK' or header is None:
                    raise SourceError('imap_candidate_header_failed')
                if not monitor_message(email.message_from_bytes(header, policy=policy.default), self.config.recipient):
                    last = uid
                    continue
                raw = self.fetch_message(client, uid)
                if raw is not None:
                    route, binding, feedback = None, None, None
                    reply = parse_reply(raw, self.config.recipient, verified_references, known_codes)
                    if not reply:
                        candidate = reply_candidate(raw, self.config.recipient)
                        if candidate:
                            proposed, reference = candidate
                            code = proposed["code"]
                            if code in known_codes and self.verify_reference(client, code, reference):
                                verified_references.setdefault(code, set()).add(reference)
                                reply = parse_reply(raw, self.config.recipient, verified_references)
                        else:
                            short = short_reply_candidate(raw, self.config.recipient)
                            if short:
                                _, reference = short
                                code = self.resolve_short_reference(client, reference, known_codes)
                                if code:
                                    verified_references.setdefault(code, set()).add(reference)
                                    reply = parse_reply(raw, self.config.recipient, verified_references, known_codes)
                    if reply:
                        replies.append(reply)
                        route = 'issued_invitation'
                    else:
                        command = notice_command(raw, self.config.recipient)
                        msg = email.message_from_bytes(raw, policy=policy.default)
                        binding = received_binding(self.root, original_subject(msg.get('Subject', ''))) if command else None
                        referenced = self.resolve_notice_reference(client, msg) if command else None
                        conflict = bool(binding and referenced and binding != referenced)
                        binding = None if conflict else (referenced or binding)
                        if binding and self.verify_sent_notice(raw):
                            notices.append(dict(command, binding_token=binding,
                                                digest=sent_reply_identity(raw, self.config.recipient)))
                            route = 'exact_notice'
                        else:
                            feedback = unhandled_reply(raw, self.config.recipient)
                            if feedback:
                                if binding:
                                    feedback['reason'] = 'sent_copy_unverified'
                                elif conflict:
                                    feedback['reason'] = 'reply_reference_conflict'
                                unhandled.append(feedback)
                                route = 'feedback'
                    if route:
                        parsed = email.message_from_bytes(raw, policy=policy.default)
                        observations.append(dict(digest=hashlib.sha256(raw).hexdigest(), uid=uid,
                            route=route, reference_present=bool(parsed.get('In-Reply-To')),
                            command_recognized=bool(notice_command(raw, self.config.recipient)),
                            binding_token=binding, code=reply.get('code') if reply else None))
                last = uid
            return {"validity": validity, "last_uid": last, "replies": replies,
                    'notices': notices, 'unhandled': unhandled, 'observations': observations}
        except Exception as error:
            self.close(broken=True)
            # Classify errors without exposing a server response, credential or mail body.
            if isinstance(error, SourceError):
                raise
            if isinstance(error, ssl.SSLCertVerificationError):
                code = "imap_tls_verification_failed"
            elif isinstance(error, socket.gaierror):
                code = "imap_dns_failed"
            elif isinstance(error, TimeoutError):
                code = "imap_timeout"
            elif isinstance(error, imaplib.IMAP4.abort):
                code = "imap_disconnected"
            elif isinstance(error, imaplib.IMAP4.error):
                # Inspect only in memory; never save the raw IMAP response.
                response_text = str(error).lower()
                limited = any(word in response_text for word in ("frequency", "too many", "rate limit", "频率"))
                code = "imap_rate_limited" if limited else (
                    "imap_login_rejected" if stage == "login" else "imap_" + stage + "_rejected")
            elif isinstance(error, OSError):
                code = "imap_network_failed"
            else:
                code = "imap_configuration_invalid" if stage == "configuration" else "imap_invalid_response"
            raise SourceError(code) from None
        finally:
            if not self.keepalive:
                self.close()
