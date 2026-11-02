import fcntl
import signal
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from .mailer import SMTPMailer, PreviewMailer, deliver
from .model import AuthNeeded, SourceError
from .source import Source
from .store import Store
from .registration_health import error_code


@contextmanager
def worker_lock(root):
    with (root / "state/worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("已有监测进程运行，不能重复启动") from None
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run(root, config):
    if not config.mail_enabled and config.mail_transport != "preview":
        raise ValueError("邮件尚未启用，请先核对 SMTP 配置并设置 mail_enabled")
    source = Source(root, config)
    mailer = SMTPMailer(root, config) if config.mail_transport == "smtp" else PreviewMailer(root / "previews")
    stopping = [False]
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.__setitem__(0, True))
    with worker_lock(root):
        store = Store(root / "state/monitor.sqlite3")
        pool = ThreadPoolExecutor(max_workers=1)
        future, next_poll = None, 0
        signup_thread = None
        execution_thread = None
        next_signup_restart = 0
        next_execution_restart = 0
        if config.signup_enabled:
            from .signup_service import run_signup
            signup_thread = threading.Thread(target=run_signup, args=(root, config, stopping), daemon=True)
            signup_thread.start()
            from .submission_lane import run_submission
            execution_thread = threading.Thread(target=run_submission, args=(root, config, stopping), daemon=True)
            execution_thread.start()
        print("监测进程已启动；正在进行实际检查，健康状态以 status 命令为准。", flush=True)
        try:
            while not stopping[0]:
                now = int(time.time())
                if config.signup_enabled and not signup_thread.is_alive() and now >= next_signup_restart:
                    print("报名确认线程已退出，正在重新启动；原有授权和提交记录保留。", flush=True)
                    signup_thread = threading.Thread(target=run_signup, args=(root, config, stopping), daemon=True)
                    signup_thread.start()
                    next_signup_restart = now + 30
                if config.signup_enabled and not execution_thread.is_alive() and now >= next_execution_restart:
                    execution_thread = threading.Thread(target=run_submission, args=(root, config, stopping), daemon=True)
                    execution_thread.start()
                    next_execution_restart = now + 30
                with store.db:
                    store.set_meta("worker_heartbeat", now)
                if future and future.done():
                    try:
                        records = future.result()
                        store.apply(records, now, config.reminder_minutes)
                        print("完整检查成功：%d 场；检查时刻 %d" % (len(records), now), flush=True)
                        next_poll = time.monotonic() + config.poll_seconds
                    except Exception as error:
                        code = error_code(error)
                        store.failure(code, now, isinstance(error, AuthNeeded))
                        failures = int(store.get_meta("failures", "1"))
                        next_poll = time.monotonic() + min(config.poll_seconds, 60 * 2 ** min(failures - 1, 3))
                        print("检查未成功：" + code, flush=True)
                    future = None
                if future is None and time.monotonic() >= next_poll:
                    with store.db:
                        store.set_meta("last_attempt", now)
                    future = pool.submit(source.snapshot, now)
                # Network checks run independently, so reminders are not blocked by slow pages.
                delivered = deliver(store, mailer, now, time_budget=15)
                if delivered:
                    label = "SMTP 已接受" if config.mail_transport == 'smtp' else "已生成邮件预览"
                    print(label + " %d 封。" % delivered, flush=True)
                end = time.monotonic() + config.tick_seconds
                while not stopping[0] and time.monotonic() < end:
                    time.sleep(min(0.5, max(0, end - time.monotonic())))
        finally:
            stopping[0] = True
            if signup_thread:
                signup_thread.join(timeout=180)
            if execution_thread:
                execution_thread.join(timeout=180)
            pool.shutdown(wait=True)
            store.close()
            print("监测进程已停止。", flush=True)
