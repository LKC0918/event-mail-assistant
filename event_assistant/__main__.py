"""Offline-first CLI. Live mail requires explicit configuration and environment secrets."""
import argparse
import email
import json
import os
import tempfile
import time
from dataclasses import replace
from email import policy
from email.message import EmailMessage
from pathlib import Path

from .config import Config, prepare, secure_json
from .inbox import message_id, notice_command, original_subject, sent_reply_identity
from .mailer import PreviewMailer, SMTPMailer, deliver
from .model import SourceError
from .notification_reply import prepare_binding, received_binding
from .registration import Registration, SignupClient
from .source import DemoAdapter, Source, sample_events
from .store import Store


def demo(root):
    """A fast, fully local end-to-end run with a simulated user reply."""
    root = Path(tempfile.mkdtemp(prefix='demo-', dir=root / 'state'))
    prepare(root)
    config = Config(signup_enabled=True)
    config.save(root)
    now = int(time.time())
    raw = sample_events(now)[0]
    raw['signup_start_time'] = now-1
    secure_json(root / 'state/demo-events.json', [raw])
    source, store = Source(root, config), Store(root / 'state/monitor.sqlite3')
    tasks, client = Registration(store), SignupClient(source)
    try:
        profile = client.profile()
        event = client.detail(raw['id'], profile)
        with store.db:
            store.set_meta('registration_current_account_hash', event['account_hash'])
        binding = prepare_binding(store, 'demo-original', 'notice_test', {'event': event}, {},
                                  '【活动助手】发现新活动｜演示活动A', now)
        with store.db:
            store.db.execute('UPDATE notification_bindings SET sent_at=? WHERE token=?', (now,binding['token']))
        reply = EmailMessage()
        reply['From'] = reply['To'] = config.sender
        reply['Date'] = email.utils.formatdate(now, usegmt=True)
        reply['Subject'] = 'Re:' + binding['subject']
        reply['In-Reply-To'] = message_id('demo-original', config.sender)
        reply.set_content('报名这个')
        command = notice_command(reply.as_bytes(), config.sender)
        token = received_binding(root, original_subject(reply['Subject']))
        assert command and token == binding['token']
        digest = sent_reply_identity(reply.as_bytes(), config.sender)
        tasks.queue_notice(token, digest, now=now, **command)
        preview = PreviewMailer(root / 'previews')
        deliver(store, preview, now)
        tasks.process_notices(client, now)
        deliver(store, preview, now)
        tasks.execute_due(client, now, enabled=True)
        tasks.reconcile(client, now+4)
        deliver(store, preview, now+4)
        row = tasks.rows()[0]
        result = {'offline_demo':True,'synthetic_reply':True,'state':row['state'],
                  'email_previews':len(list((root/'previews').glob('*.html'))),
                  'real_emails_sent':0,'real_network_requests':0,'real_registrations_submitted':0,
                  'preview_directory':str(root/'previews')}
        assert row['state'] == 'success'
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        store.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='活动监测与邮件确认助手')
    parser.add_argument('--root',type=Path,default=Path.cwd(),help='自己的配置及运行数据目录')
    parser.add_argument('command',choices=('init','demo','check','run','status'))
    args = parser.parse_args()
    root = args.root.resolve()
    prepare(root)
    try:
        config = Config.load(root)
        if args.command == 'init':
            if not (root/'config.json').exists():config.save(root)
            if config.adapter == 'demo':DemoAdapter.initialize(root)
            print('初始化完成：默认仅离线演示和邮件预览；未启用真实邮箱或真实报名。')
        elif args.command == 'demo':
            demo(root)
        elif args.command == 'check':
            rows = Source(root,config).snapshot(int(time.time()))
            print(json.dumps({'complete_snapshot':True,'events':len(rows),'sent_mail':False,'submitted':False}))
        elif args.command == 'run':
            from .service import run
            run(root,config)
        else:
            path=root/'state/monitor.sqlite3'
            if not path.exists():
                print(json.dumps({'running':False,'reason':'no_runtime_state'}));return 1
            store=Store(path)
            try:
                now=int(time.time());status=store.status(now)
                heartbeat=int(store.get_meta('worker_heartbeat') or 0)
                status.update(mail_transport=config.mail_transport,signup_enabled=config.signup_enabled,
                              worker_age_seconds=now-heartbeat if heartbeat else None,
                              executor_mode=store.get_meta('registration_executor_mode'),
                              executor_error=store.get_meta('registration_executor_error') or None)
                status['healthy']=bool(status['last_success'] and now-heartbeat<90 and not status['source_error'] and not status['mail_error'])
                if config.signup_enabled:
                    status['healthy']=bool(status['healthy'] and now-int(store.get_meta('registration_executor_heartbeat') or 0)<45 and not status['executor_error'])
                print(json.dumps(status,ensure_ascii=False,indent=2))
                return 0 if status['healthy'] else 1
            finally:store.close()
    except (SourceError,ValueError,OSError) as error:
        # Show safe codes only, never authenticated URLs or provider messages.
        print(json.dumps({'ok':False,'error_type':type(error).__name__},ensure_ascii=False))
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
