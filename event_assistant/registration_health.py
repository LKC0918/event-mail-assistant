"""Persistent, privacy-safe incident tracking for signup reads and reply polling."""
import json
import re

from .model import SourceError, display_time

AREAS = {"catalog": "活动报名信息读取", "inbox": "邮件 邮件收信", "worker": "报名确认服务"}
URGENT = {"imap_login_rejected", "imap_configuration_invalid", "imap_tls_verification_failed"}
EXPLANATIONS = {
    'network_error': '读取活动平台报名信息时通信失败，系统正在重试。',
    'network_timeout': '读取活动平台报名信息超时，系统正在重试。',
    'connection_error': '活动平台报名信息连接暂时失败，系统正在重试。',
    'dns_error': '活动平台网站域名暂时无法解析，系统正在重试。',
    'scan_timeout': '本轮报名信息未能在限定时间内完整读取，已保留已有授权并重新核验。',
    "imap_timeout": "连接 邮件 收信服务器超时，系统正在自动重试。",
    "imap_disconnected": "邮件 收信连接已断开，系统正在自动重新连接。",
    "imap_network_failed": "连接 邮件 收信服务器失败，系统正在自动重试。",
    "imap_dns_failed": "暂时无法解析 邮件 收信服务器地址，系统正在自动重试。",
    "imap_rate_limited": "邮件 收信服务器暂时限制连接，系统已延长重试间隔。",
    "imap_login_rejected": "邮件 邮箱拒绝登录，请检查 IMAP 服务是否开启、专用授权码是否仍有效。",
    "imap_configuration_invalid": "邮件 收信配置缺失、格式或权限不正确，需要修复配置。",
    "imap_tls_verification_failed": "邮件 收信连接的安全证书校验失败，请检查服务器网络和时间。",
}


def error_code(error):
    if isinstance(error, SourceError) and re.fullmatch(r"[A-Za-z0-9_:-]{1,96}", str(error)):
        return str(error)
    return type(error).__name__


def report_error(store, area, error, now):
    label = AREAS[area]
    code = error_code(error)
    prefix = "registration_" + area
    with store.db:
        start = int(store.get_meta(prefix + "_incident_start", "0") or 0) or now
        count = int(store.get_meta(prefix + "_failures", "0") or 0) + 1
        store.set_meta(prefix + "_error", code)
        store.set_meta(prefix + "_incident_start", start)
        store.set_meta(prefix + '_recovery_successes', 0)
        store.set_meta(prefix + '_recovery_first_at', '')
        store.set_meta(prefix + "_failures", count)
        store.set_meta(prefix + "_last_failure_at", now)
        store.set_meta(prefix + "_last_failure_code", code)
        store.set_meta(prefix + "_total_failures", int(store.get_meta(prefix + "_total_failures", "0") or 0) + 1)
        if count == 1 or now - int(store.get_meta(prefix + "_last_log_at", "0") or 0) >= 300:
            print(json.dumps({"component": "registration", "area": area, "event": "failure",
                              "code": code, "consecutive_failures": count, "incident_start": start},
                             ensure_ascii=False), flush=True)
            store.set_meta(prefix + "_last_log_at", now)
        # A temporary glitch is retried quietly. Genuine auth/config errors need immediate action.
        if code not in URGENT and area != "worker" and (count < 3 or now - start < 120):
            return
        key = "registration-error:%s:%s" % (area, start)
        store.set_meta(prefix + "_incident_key", key)
        store.set_meta(prefix + "_alert_start", start)
        data = {"subject": label + "需要处理", "health_area": area,
                      "lines": [label + "持续异常。", "开始时间：" + display_time(start),
                                EXPLANATIONS.get(code, "系统无法完整完成此项核验，正在重试；需要检查服务记录。"),
                                "故障类型：" + code,
                                "活动监测与收信是独立任务；本通知不代表活动列表读取也已失败。",
                                "未确认的活动不会报名。恢复后会补读尚未处理的回复；仍须在截止前收到有效确认。",
                                "恢复正常后会再发一封恢复通知。"]}
        store.enqueue(key, 'registration', data, now, now + 86400, now)
        store.db.execute("UPDATE outbox SET status='pending',data=?,due_at=?,next_attempt=0,expires_at=? "
                         "WHERE key=? AND status IN ('cancelled','expired')",
                         (json.dumps(data, ensure_ascii=False), now, now + 86400, key))


def report_success(store, area, now):
    label = AREAS[area]
    prefix = "registration_" + area
    with store.db:
        start = int(store.get_meta(prefix + "_incident_start", "0") or 0)
        count = int(store.get_meta(prefix + "_failures", "0") or 0)
        key = store.get_meta(prefix + "_incident_key")
        alert = store.db.execute("SELECT status FROM outbox WHERE key=?", (key,)).fetchone() if key else None
        # Also cancel queued alerts from the older hourly-bucket implementation.
        store.db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' AND key LIKE ?",
                         ("registration-error:" + area + ":%",))
        if area == 'catalog' and start:
            successes = int(store.get_meta(prefix + '_recovery_successes', '0') or 0) + 1
            first = int(store.get_meta(prefix + '_recovery_first_at', '0') or 0) or now
            store.set_meta(prefix + '_recovery_successes', successes)
            store.set_meta(prefix + '_recovery_first_at', first)
            if successes < 3 or now - first < 300:
                store.set_meta(prefix + '_error', '')
                store.set_meta(prefix + '_failures', 0)
                store.set_meta(prefix + '_last_success_at', now)
                return
        if alert and alert[0] == "sent":
            alert_start = int(store.get_meta(prefix + "_alert_start", "0") or 0) or start
            store.enqueue("registration-recovery:%s:%s" % (area, alert_start), "registration",
                          {"subject": label + "已恢复", "health_area": area,
                           "lines": [label + "已成功完成检查，已恢复正常。",
                                     "恢复时间：" + display_time(now),
                                     "系统继续读取尚未处理的回复；每场仍须你的明确邮件确认才会提交报名。"]},
                          now, now + 86400, now)
            store.set_meta(prefix + "_incident_key", "")
            store.set_meta(prefix + "_alert_start", "")
        if count:
            print(json.dumps({"component": "registration", "area": area, "event": "recovered",
                              "failed_attempts": count, "duration_seconds": now - start},
                             ensure_ascii=False), flush=True)
        store.set_meta(prefix + "_error", "")
        store.set_meta(prefix + "_failures", 0)
        store.set_meta(prefix + "_incident_start", "")
        store.set_meta(prefix + '_recovery_successes', 0)
        store.set_meta(prefix + '_recovery_first_at', '')
        # Retain the cancelled alert reference: SMTP may already be sending it.
        # If that late send completes, the next successful poll issues its recovery.
        store.set_meta(prefix + "_last_success_at", now)


def inbox_retry_delay(store, normal):
    """Bound retries to two minutes, with slower attempts for rejected credentials."""
    failures = int(store.get_meta("registration_inbox_failures", "0") or 0)
    code = store.get_meta("registration_inbox_error")
    if not failures:
        return normal
    if code in URGENT or code == "imap_rate_limited":
        return 120
    return min(120, normal * 2 ** min(failures - 1, 3))
