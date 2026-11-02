import hashlib
import html
import json
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate

from .config import secure_json, mail_password
from .inbox import message_id
from .model import APP, display_time, signup_status
from .notification_reply import prepare_binding

SOURCE_EXPLANATIONS = {
    'network_error': '与活动平台网站通信暂时失败。',
    'network_timeout': '读取活动平台网站超时。',
    'connection_error': '暂时无法连接活动平台网站。',
    'dns_error': '活动平台网站域名暂时无法解析。',
    'tls_verification_failed': '网站安全证书校验失败，需要检查服务器时间和网络。',
    'scan_timeout': '本轮完整读取超时，已保留此前成功读取的活动。',
    'list_changed_during_scan': '网站列表在分页期间变化，系统正在重新完整读取。',
    'http_429': '活动平台网站暂时限制了请求频率。',
    'http_502': '活动平台网站或网关暂时不可用。',
    'http_503': '活动平台网站暂时不可用。',
    'http_504': '活动平台网站网关响应超时。',
}


def render(kind, data, events, now, last_success=0):
    selected = [events[x] for x in data.get("ids", []) if x in events]
    headings = {"initial": "首次活动汇总", "new": "发现新活动", "signup_time": "报名时间已公布",
                "reminder": "报名即将开始", "alert": "监测需要处理", "recovery": "监测已恢复",
                "test": "发信测试", "registration": "报名确认与结果", "inbox_test": "收信确认测试"}
    if kind == 'alert':
        headings['alert'] = '监测需要你登录' if data.get('auth') else (
            '监测需要处理' if data.get('reason') == 'tls_verification_failed' else '活动读取暂时中断')
    elif kind == 'recovery':
        headings['recovery'] = '活动读取已稳定恢复'
    if kind == "registration" and data.get("event"):
        selected = [data["event"]]
    title = data.get("subject", headings[kind])
    if len(selected) == 1 and kind not in ("initial",) and not data.get("subject"):
        title += "｜" + selected[0]["title"]
    lines = [headings[kind], "所有时间均为北京时间（UTC+8）。", ""]
    if kind == "inbox_test":
        lines += data.get("lines", []) + [""]
    if kind == "initial":
        lines += ["首次启动时发现 %d 场尚未结束或结束时间未公布的活动。" % len(selected),
                  "报名尚未开始的活动已纳入提前 15 分钟提醒。", ""]
    if kind == "alert":
        lines += [("活动平台登录未能自动恢复，需要你重新完成官方登录。" if data.get("auth") else
                   "连续读取网站失败，暂时无法确认是否出现了新活动。"),
                  "最近成功检查：" + display_time(last_success),
                  "异常开始：" + display_time(data.get('incident_start')),
                  SOURCE_EXPLANATIONS.get(data.get('reason'), '系统暂时未能完成一次完整读取，正在重新检查。'),
                  "故障类型：" + str(data.get('reason', 'unknown')),
                  ("处理方法：重新完成活动平台官方登录并同步登录状态。" if data.get('auth') else
                   "系统正在重试并保留已知活动；网络异常不需要先重配邮箱或重新登录。"),
                  "已知活动的提醒仍会基于最近一次成功记录发送，并标注信息时间。",
                  "恢复读取后会发一封恢复通知。", APP]
    elif kind == "recovery":
        lines += ["活动列表已连续成功完成检查，监测已恢复。",
                  "最近成功检查：" + display_time(data.get('recovered_at') or last_success),
                  "恢复读取后发现、且仍在网站列表中的新活动会继续通知。"]
    elif kind == "test":
        lines += ["这是你授权发送的测试邮件。收到此邮件只能说明发信链路可用，不能代表云端监测已经启用。"]
    if selected:
        lines += ["最近成功核验：" + display_time(last_success)]
        if now - last_success > 600:
            lines += ["注意：网站近期未能成功刷新，以下为已保存的信息，请打开原文确认。"]
    for item in sorted(selected, key=lambda x: (x["start_time"] or 10**12, x["id"])):
        start = item["signup_start_time"]
        lines += ["", item["title"],
                  "报名开始：" + display_time(start),
                  "报名截止：" + display_time(item["signup_end_time"]),
                  "活动开始：" + display_time(item["start_time"]),
                  "活动结束：" + display_time(item["end_time"]),
                  "当前时间状态：" + signup_status(item, now)]
        if start and 0 < start - now <= 900:
            lines += ["距报名开始约 %d 分钟，请提前准备。" % max(1, (start - now + 59) // 60)]
        lines += ["详情与报名入口：" + item.get("url", APP + "events/" + item["id"])]
    if kind == "registration":
        lines += [""] + data.get("lines", [])
    elif kind in ('new', 'reminder', 'signup_time'):
        lines += ['', '想参加请直接回复这封邮件，正文第一行写：报名这个。',
                  '只有一种参加方式且无时间冲突时，系统会确认并发送“已安排报名”回执。',
                  '有多个参加方式、地点或时间冲突时，会再发邮件请你选择；你可以直接回复那封询问。',
                  '不参加可回复：不报名。无需复制编号，也不必另找“待确认报名”邮件。',
                  '“已安排报名”表示已确认；收到“报名成功”才表示官网结果已核实。']
    elif kind == 'initial':
        lines += ['', '想参加某场活动，请直接回复该场单独的活动提醒，第一行写“报名这个”。',
                  '这份汇总包含多场时不会猜测你选择哪场。多个参加方式或时间冲突会另发邮件询问。']
    lines += ["", "活动监测助手 · 请以活动平台原页面最新信息为准。"]
    text = "\n".join(lines)
    paragraphs = []
    for line in lines:
        if line.startswith("详情与报名入口："):
            url = line.split("：", 1)[1]
            paragraphs.append('<p><a style="color:#176454" href="%s">查看活动详情及报名入口 →</a></p>' % html.escape(url, quote=True))
        else:
            paragraphs.append('<p style="margin:8px 0;line-height:1.75">%s</p>' % (html.escape(line) or "&nbsp;"))
    markup = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
              '<meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(title) +
              '</title></head><body style="margin:0;background:#f4f5f1;color:#23342f;font:16px -apple-system,sans-serif">'
              '<main style="max-width:640px;margin:24px auto;padding:28px;background:white;border-top:5px solid #176454">'
              '<div style="font-size:12px;letter-spacing:2px;color:#176454">活动平台 · 活动助手</div>'
              '<h1 style="font-size:24px;line-height:1.4">' + html.escape(headings[kind]) + '</h1>' +
              ''.join(paragraphs[1:]) + '</main></body></html>')
    if data.get('consent_token'):
        # MIME source metadata binds 邮件-rewritten parent IDs without displaying
        # an internal nonce or asking the user to copy it.
        import re
        token = data['consent_token']
        if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{32}', token):
            raise ValueError('invalid_consent_metadata')
        markup = markup.replace('</main>', '<!-- event-consent:' + token + ' --></main>')
    if data.get('notification_token'):
        markup = markup.replace('</main>', '<!-- event-notice:' + data['notification_token'] + ' --></main>')
    subject = ("[EVENTS] " if kind in ("registration", "inbox_test") else "") + "【活动助手】" + title
    return data.get('bound_subject') or subject, text, markup


class SMTPMailer:
    def __init__(self, root, config):
        self.root, self.config = root, config

    def send(self, key, subject, text, markup):
        if not self.config.mail_enabled:
            raise ValueError("mail_not_enabled")
        password = mail_password(self.config)
        if not password:
            raise ValueError("missing_smtp_authorization")
        message = EmailMessage()
        message["From"], message["To"] = self.config.sender, self.config.recipient
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = message_id(key, self.config.sender)
        message["Reply-To"] = self.config.recipient
        if key.startswith('consent:'):
            import re
            token = key.split(':', 1)[1]
            if re.fullmatch(r'[0-9a-f]{32}', token):
                message['X-EVENTS-Consent'] = token
        message.set_content(text)
        message.add_alternative(markup, subtype="html")
        # Do not use a context manager: some SMTP servers disconnect on QUIT after
        # accepting DATA; that must not turn an accepted email into a failed send.
        smtp = smtplib.SMTP_SSL(self.config.smtp_host, self.config.smtp_port,
                              timeout=10, context=ssl.create_default_context())
        try:
            smtp.login(self.config.sender, password)
            refused = smtp.send_message(message, from_addr=self.config.sender, to_addrs=[self.config.recipient])
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        finally:
            try:
                smtp.close()
            except OSError:
                pass


class PreviewMailer:
    def __init__(self, path):
        self.path = path

    def send(self, key, subject, text, markup):
        self.path.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(key.encode()).hexdigest()[:12]
        (self.path / (name + ".html")).write_text(markup, encoding="utf-8")
        secure_json(self.path / (name + ".json"), {"preview_only": True, "subject": subject, "text": text})


def deliver(store, mailer, now, time_budget=None):
    """Single dispatcher; failures remain durable, retries never advance the baseline."""
    count = 0
    started = time.monotonic()
    for row in store.due(now):
        if time_budget is not None and time.monotonic() - started >= time_budget:
            break
        if store.was_covered(row, now):
            continue
        data = json.loads(row["data"])
        events = store.events(present_only=True)
        subject, text, markup = render(row["kind"], data, events, now,
                                     int(store.get_meta("last_success", "0")))
        binding = None
        if getattr(getattr(mailer, 'config', None), 'signup_enabled', False):
            binding = prepare_binding(store, row['key'], row['kind'], data, events, subject, now)
            if binding:
                data = dict(data, bound_subject=binding['subject'], notification_token=binding['token'])
                subject, text, markup = render(row['kind'], data, events, now,
                                             int(store.get_meta('last_success', '0')))
        try:
            mailer.send(row["key"], subject, text, markup)
        except Exception as e:
            # Never record credentials, SMTP server response bodies or SSO callback URLs.
            store.mail_failure(row, type(e).__name__, now)
            break
        facts = {x: events[x]["signup_start_time"] for x in data.get("ids", []) if x in events}
        if binding:
            store.sent(row, now, facts, notification_token=binding['token'])
        else:
            store.sent(row, now, facts)
        count += 1
    return count
