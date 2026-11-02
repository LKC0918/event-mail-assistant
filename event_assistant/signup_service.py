import time
from concurrent.futures import ThreadPoolExecutor

from .config import Config
from .inbox import MailInbox
from .registration import Registration, SignupClient
from .registration_health import inbox_retry_delay, report_error, report_success
from .source import Source
from .store import Store


def run_signup(root, config, stopping):
    store = Store(root / "state/monitor.sqlite3")
    tasks = Registration(store)
    client = SignupClient(Source(root, config))
    catalog_client = SignupClient(Source(root, config))
    inbox = MailInbox(root, config, keepalive=True)
    pool = ThreadPoolExecutor(max_workers=2)
    catalog_future, inbox_future = None, None
    next_catalog, next_inbox = 0, 0
    worker_healthy = False
    try:
        while not stopping[0]:
            now = int(time.time())
            enabled = Config.load(root).signup_enabled
            with store.db:
                store.set_meta("registration_heartbeat", now)
            if not enabled:
                # Disabling halts new writes; already-submitted results are still reconciled.
                tasks.reconcile(client, now)
                if not worker_healthy:
                    report_success(store, "worker", now)
                    worker_healthy = True
                time.sleep(1)
                continue
            if catalog_future and catalog_future.done():
                try:
                    items, registered = catalog_future.result()
                    tasks.sync(items, registered, now)
                    report_success(store, "catalog", now)
                except Exception as e:
                    report_error(store, "catalog", e, now)
                failures = int(store.get_meta('registration_catalog_failures', '0') or 0)
                delay = min(config.poll_seconds, 60 * 2 ** min(failures - 1, 2)) if failures else config.poll_seconds
                catalog_future, next_catalog = None, now + delay
            if inbox_future and inbox_future.done():
                try:
                    result = inbox_future.result()
                    # Apply decisions and advance the cursor in one transaction.
                    # reply commits internally; replay dedup covers a crash before cursor advance.
                    for reply in result["replies"]:
                        tasks.reply(now=now, **reply)
                    for intent in result.get('notices', ()):
                        tasks.queue_notice(now=now, **intent)
                    for feedback in result.get('unhandled', ()):
                        tasks.reply_help(now=now, **feedback)
                    with store.db:
                        for observation in result.get('observations', ()):
                            tasks.record_transport(observation, now)
                        store.set_meta("imap_uid", result["last_uid"])
                        store.set_meta("imap_validity", result["validity"])
                        store.set_meta("imap_last_success", now)
                    report_success(store, "inbox", now)
                except Exception as e:
                    report_error(store, "inbox", e, now)
                inbox_future, next_inbox = None, now + inbox_retry_delay(store, config.imap_poll_seconds)
            # Opening-time execution runs in submission_lane with its own DB and
            # HTTP readers; these slow decisions cannot occupy its timer.
            tasks.process_notices(client, now)
            tasks.reconcile(client, now)
            if catalog_future is None and now >= next_catalog:
                # Reuse a separate catalog client, and the main worker's completed
                # snapshot when fresh, rather than re-reading every page twice.
                last = int(store.get_meta('last_success', '0') or 0)
                base = list(store.events(present_only=True).values()) if last and now - last <= config.poll_seconds else None
                catalog_future = pool.submit(catalog_client.catalog, now, base)
            if config.mail_transport == "smtp" and config.mail_enabled and inbox_future is None and now >= next_inbox:
                codes = {row["token"] for row in tasks.rows() if row["state"] in ("pending", "approved")}
                inbox_future = pool.submit(inbox.poll, int(store.get_meta("imap_uid", "0")),
                                           store.get_meta("imap_validity"), codes)
            # Starting a thread does not prove the worker has recovered. Persistent
            # startup/loop faults must remain one incident across supervisor restarts.
            if not worker_healthy:
                report_success(store, "worker", now)
                worker_healthy = True
            time.sleep(0.5)
    except Exception as e:
        report_error(store, "worker", e, int(time.time()))
    finally:
        pool.shutdown(wait=True)
        inbox.close()
        store.close()
