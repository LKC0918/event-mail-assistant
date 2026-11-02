"""Public configuration and environment-only mail credentials."""
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Config:
    adapter: str = 'demo'
    source_file: str = 'examples/events.json'
    sender: str = 'you@example.com'
    recipient: str = 'you@example.com'
    smtp_host: str = 'smtp.example.com'
    smtp_port: int = 465
    imap_host: str = 'imap.example.com'
    imap_port: int = 993
    sent_folder: str = '"Sent"'
    mail_password_env: str = 'EVENT_MAIL_PASSWORD'
    mail_transport: str = 'preview'
    mail_enabled: bool = False
    signup_enabled: bool = False
    allow_live_submissions: bool = False
    poll_seconds: int = 300
    reminder_minutes: int = 15
    tick_seconds: int = 10
    imap_poll_seconds: int = 30
    request_timeout_seconds: int = 15
    scan_timeout_seconds: int = 120
    page_size: int = 100
    max_pages: int = 30

    @classmethod
    def load(cls, root):
        path = Path(root) / 'config.json'
        result = cls(**json.loads(path.read_text())) if path.exists() else cls()
        if result.mail_transport not in ('preview', 'smtp'):
            raise ValueError('invalid_mail_transport')
        if not (15 <= result.imap_poll_seconds <= 120 and 1 <= result.tick_seconds <= 30
                and 60 <= result.poll_seconds <= 600 and 1 <= result.reminder_minutes <= 60):
            raise ValueError('invalid_poll_intervals')
        for address in (result.sender, result.recipient):
            if '@' not in address or any(c in address for c in '\r\n,;'):
                raise ValueError('invalid_mail_address')
        if result.signup_enabled and result.sender.lower() != result.recipient.lower():
            raise ValueError('confirmation_requires_one_mail_account')
        if result.sent_folder.count('"') not in (0, 2) or any(c in result.sent_folder for c in '\r\n'):
            raise ValueError('invalid_sent_folder')
        return result

    def save(self, root):
        secure_json(Path(root) / 'config.json', asdict(self))


def secure_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.writing-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def read_secret(path):
    path = Path(path)
    if not path.is_file(): raise ValueError('missing_local_file')
    if path.stat().st_mode & 0o077: raise ValueError('private_file_permissions_must_be_600')
    return json.loads(path.read_text())


def mail_password(config):
    value = os.environ.get(config.mail_password_env, '')
    if not value or '\r' in value or '\n' in value:
        raise ValueError('missing_mail_password_environment')
    return value


def prepare(root):
    for name in ('state', 'previews'):
        path = Path(root) / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
