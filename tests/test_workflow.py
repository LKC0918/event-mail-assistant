"""Synthetic fixtures only; every test blocks socket connections."""
import contextlib
import copy
import io
import json
import re
import socket
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from event_assistant.__main__ import demo
from event_assistant.config import Config, prepare, secure_json
from event_assistant.inbox import notice_command, original_subject, sent_reply_identity
from event_assistant.model import SourceError, normalize, timestamp
from event_assistant.notification_reply import prepare_binding, received_binding
from event_assistant.registration import Registration, SignupClient, registration_view
from event_assistant.source import DemoAdapter, Source, sample_events
from event_assistant.store import Store
from event_assistant.submission_lane import OpeningClient, opening_candidates, prepare as readiness, run_submission
from event_assistant.signup_service import run_signup


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.network = contextlib.ExitStack()
        for name in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex'):
            self.network.enter_context(patch(name, side_effect=AssertionError('network_forbidden_in_tests')))
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        prepare(self.root)
        self.now = int(time.time())
        self.raw = sample_events(self.now)[0]
        self.raw['signup_start_time'] = self.now-1
        self.config = Config(signup_enabled=True)
        self.config.save(self.root)
        secure_json(self.root/'state/demo-events.json', [self.raw])
        self.source = Source(self.root, self.config)
        self.client = SignupClient(self.source)
        self.store = Store(self.root/'state/monitor.sqlite3')
        self.tasks = Registration(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
        self.network.close()

    def item(self, raw=None):
        return registration_view(raw or self.raw, self.client.profile())

    def row(self, identity='1001'):
        return self.store.db.execute('SELECT * FROM registrations WHERE event_id=?', (identity,)).fetchone()

    def approve(self, item=None):
        item = item or self.item()
        with self.store.db:
            self.tasks.request(item, [], self.now)
        self.assertEqual(self.tasks.reply(self.row(item['id'])['token'], None, False, False, 'a'*64, self.now), 'approved')
        return self.row(item['id'])

    def binding(self, item=None):
        item = item or self.item()
        with self.store.db:
            self.store.set_meta('registration_current_account_hash', item['account_hash'])
        binding = prepare_binding(self.store, 'original', 'new', {'event':item}, {},
                                  '【活动助手】发现新活动｜'+item['title'], self.now)
        with self.store.db:
            self.store.db.execute('UPDATE notification_bindings SET sent_at=? WHERE token=?', (self.now,binding['token']))
        return binding

    def command(self, subject='Re: reminder', body='报名这个', sender='you@example.com'):
        msg=EmailMessage();msg['From']=sender;msg['To']='you@example.com'
        msg['Subject']=subject;msg['Date']='Mon, 02 Nov 2026 12:00:00 +0000'
        msg.set_content(body)
        return msg.as_bytes()


class ModelTests(OfflineCase):
    def test_times_are_explicitly_normalized(self):
        self.assertEqual(timestamp('2030-01-01 08:00:00'),timestamp('2030-01-01T00:00:00Z'))
        self.assertEqual(timestamp(1893456000000),1893456000)

    def test_invalid_time_fails_closed(self):
        with self.assertRaises(SourceError):timestamp('sometime tomorrow')

    def test_invalid_id_rejected(self):
        raw=dict(self.raw,id='../private')
        with self.assertRaises(SourceError):normalize(raw)

    def test_public_links_cannot_carry_auth(self):
        for address in ('https://person:secret@example.com/a','https://example.com/a?token=example', 'https://example.com/a#secret'):
            with self.subTest(address=address), self.assertRaises(SourceError):normalize(dict(self.raw,url=address))

    def test_private_profile_not_copied_to_event(self):
        self.assertNotIn('contact', normalize(dict(self.raw,contact='private placeholder')))

    def test_reversed_schedule_rejected(self):
        with self.assertRaises(SourceError):normalize(dict(self.raw,end_time=self.now))


class StoreTests(OfflineCase):
    def test_new_event_deduplicated_across_restart(self):
        events=[normalize(self.raw)];self.store.apply(events,self.now)
        other=normalize(sample_events(self.now)[1]);events.append(other)
        self.store.apply(events,self.now+1);self.store.close()
        self.store=Store(self.root/'state/monitor.sqlite3');self.tasks=Registration(self.store)
        self.store.apply(events,self.now+2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE kind='new'").fetchone()[0],1)

    def test_invalid_snapshot_does_not_withdraw_previous(self):
        self.store.apply([normalize(self.raw)],self.now)
        with self.assertRaises(SourceError):self.store.apply([],self.now+1)
        self.assertEqual(len(self.store.events(present_only=True)),1)

    def test_reschedule_cancels_old_reminder(self):
        item=normalize(dict(self.raw,signup_start_time=self.now+1800))
        self.store.apply([item],self.now)
        item['signup_start_time']+=600;self.store.apply([item],self.now+1)
        rows=self.store.db.execute("SELECT status FROM outbox WHERE kind='reminder'").fetchall()
        self.assertCountEqual([x[0] for x in rows],['pending','cancelled'])

    def test_transient_error_does_not_send_alert(self):
        self.store.failure('network_timeout',self.now)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE kind='alert'").fetchone()[0],0)

    def test_recovery_requires_stable_successes(self):
        for delta in (0,90,180):self.store.failure('network_timeout',self.now+delta)
        row=self.store.db.execute("SELECT * FROM outbox WHERE kind='alert'").fetchone()
        self.store.sent(row,self.now+181)
        item=normalize(self.raw)
        self.store.apply([item],self.now+200)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE kind='recovery'").fetchone()[0],0)
        self.store.apply([item],self.now+350);self.store.apply([item],self.now+500)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE kind='recovery'").fetchone()[0],1)


class ReplyTests(OfflineCase):
    def test_body_short_phrase_is_recognized(self):
        self.assertEqual(notice_command(self.command(),'you@example.com')['choice'],None)

    def test_new_subject_alone_is_not_a_command(self):
        self.assertIsNone(notice_command(self.command('报名这个',''),'you@example.com'))

    def test_foreign_sender_cannot_authorize(self):
        self.assertIsNone(notice_command(self.command(sender='other@example.com'),'you@example.com'))

    def test_quote_is_not_user_command(self):
        self.assertIsNone(notice_command(self.command(body='> 报名这个'),'you@example.com'))

    def test_sent_reply_digest_stable(self):
        raw=self.command()
        self.assertEqual(sent_reply_identity(raw,'you@example.com'),sent_reply_identity(raw,'you@example.com'))

    def test_truncated_subject_resolves_issued_binding(self):
        binding=self.binding()
        shortened=binding['subject'].split('｜')[0]
        self.assertEqual(received_binding(self.root,original_subject('Re: '+shortened)),binding['token'])

    def test_unissued_tag_does_not_guess_newest(self):
        self.binding()
        self.assertIsNone(received_binding(self.root,'【活动助手】〔回信000000〕same title'))

    def test_received_approval_and_success_are_separate(self):
        binding=self.binding();command=notice_command(self.command(),'you@example.com')
        self.tasks.queue_notice(binding['token'],'b'*64,now=self.now,**command)
        subjects=[json.loads(r[0])['subject'] for r in self.store.db.execute('SELECT data FROM outbox')]
        self.assertTrue(any('已收到' in x for x in subjects));self.assertIsNone(self.row())
        self.tasks.process_notices(self.client,self.now)
        self.assertEqual(self.row()['state'],'approved')
        self.tasks.execute_due(self.client,self.now,enabled=True)
        self.assertEqual(self.row()['state'],'submitting')
        self.tasks.reconcile(self.client,self.now+4)
        self.assertEqual(self.row()['state'],'success')

    def test_multiple_modes_require_choice(self):
        raw=copy.deepcopy(self.raw);second=copy.deepcopy(raw['sessions'][0]);second.update(id=2002,mode='线下')
        second['groups'][0]['id']=20020;raw['sessions'].append(second)
        secure_json(self.root/'state/demo-events.json',[raw])
        binding=self.binding(self.item(raw))
        self.tasks.queue_notice(binding['token'],'c'*64,now=self.now,**notice_command(self.command(),'you@example.com'))
        self.tasks.process_notices(self.client,self.now)
        self.assertEqual(self.row()['state'],'pending')


class RegistrationTests(OfflineCase):
    def test_unapproved_task_never_submits(self):
        with self.store.db:self.tasks.request(self.item(),[],self.now)
        with patch.object(self.client,'submit') as submit:self.tasks.execute_due(self.client,self.now,enabled=True)
        submit.assert_not_called()

    def test_duplicate_reply_and_execution_only_one_submit(self):
        row=self.approve()
        self.assertEqual(self.tasks.reply(row['token'],None,False,False,'a'*64,self.now),'duplicate')
        with patch.object(self.client,'submit',wraps=self.client.submit) as submit:
            self.tasks.execute_due(self.client,self.now,enabled=True);self.tasks.execute_due(self.client,self.now+1,enabled=True)
        self.assertEqual(submit.call_count,1)

    def test_disable_prevents_execution(self):
        self.approve()
        with patch.object(self.client,'submit') as submit:self.tasks.execute_due(self.client,self.now,enabled=False)
        submit.assert_not_called()

    def test_revoke_before_submit(self):
        row=self.approve();self.tasks.reply(row['token'],None,False,True,'d'*64,self.now)
        self.tasks.execute_due(self.client,self.now,enabled=True)
        self.assertEqual(self.row()['state'],'declined')

    def test_full_capacity_pauses_without_post(self):
        self.approve();raw=copy.deepcopy(self.raw);raw['signup_quota']=20
        secure_json(self.root/'state/demo-events.json',[raw])
        with patch.object(self.client,'submit') as submit:self.tasks.execute_due(self.client,self.now,enabled=True)
        submit.assert_not_called();self.assertEqual(self.row()['state'],'paused')

    def test_timeout_after_post_never_resubmits(self):
        self.approve()
        with patch.object(self.client,'submit',side_effect=TimeoutError) as submit:
            self.tasks.execute_due(self.client,self.now,enabled=True)
            for step in range(1,14):
                self.tasks.reconcile(self.client,self.now+step*4)
                self.tasks.execute_due(self.client,self.now+step*4,enabled=True)
        self.assertEqual(submit.call_count,1);self.assertEqual(self.row()['state'],'uncertain')

    def test_schedule_change_revokes_previous_consent(self):
        self.approve();raw=copy.deepcopy(self.raw);raw['start_time']+=60
        secure_json(self.root/'state/demo-events.json',[raw])
        self.tasks.execute_due(self.client,self.now,enabled=True)
        self.assertEqual(self.row()['state'],'pending')

    def test_existing_conflict_requires_explicit_choice(self):
        item=self.item();other=copy.deepcopy(item);other['id']='2002'
        conflicts=self.tasks.conflicts(item,[other])
        with self.store.db:self.tasks.request(item,conflicts,self.now)
        outcome=self.tasks.reply(self.row()['token'],1,False,False,'e'*64,self.now)
        self.assertNotEqual(outcome,'approved');self.assertEqual(self.row()['state'],'pending')

    def test_official_deadline_prevents_post(self):
        self.approve()
        with patch.object(self.client,'submit') as submit:self.tasks.execute_due(self.client,self.raw['signup_end_time'],enabled=True)
        submit.assert_not_called();self.assertEqual(self.row()['state'],'expired')

    def two_items(self):
        other=copy.deepcopy(self.raw);other['id']='1002';other['title']='演示互斥活动B'
        other['sessions'][0]['id']=1002;other['sessions'][0]['groups'][0]['id']=10020
        secure_json(self.root/'state/demo-events.json',[self.raw,other])
        return [self.item(),self.item(other)]

    def test_two_choice_latch_at_most_one(self):
        items=self.two_items();self.tasks.arm_choice_waitlists(items,[],self.now,'demo-exclusive-authorization')
        with patch.object(self.client,'submit',wraps=self.client.submit) as submit:
            self.tasks.execute_due(self.client,self.now,enabled=True);self.tasks.reconcile(self.client,self.now+4)
            self.tasks.execute_due(self.client,self.now+61,enabled=True)
        self.assertEqual(submit.call_count,1)
        self.assertEqual(sum(row['state']=='success' for row in self.tasks.rows()),1)

    def test_personal_deadline_stops_both_choices(self):
        self.tasks.arm_choice_waitlists(self.two_items(),[],self.now,'demo-cutoff')
        self.tasks.set_choice_deadline('demo-cutoff',self.now+10,self.now)
        with patch.object(self.client,'submit') as submit:self.tasks.execute_due(self.client,self.now+11,enabled=True)
        submit.assert_not_called();self.assertTrue(all(r['state']=='expired' for r in self.tasks.rows()))


class AdapterTests(OfflineCase):
    def test_blocked_reply_processing_does_not_block_executor(self):
        import threading
        self.approve();entered=threading.Event();release=threading.Event();stopping=[False]
        def slow_mail(*args):
            entered.set();release.wait(5)
        with patch.object(Registration,'process_notices',side_effect=slow_mail):
            mail=threading.Thread(target=run_signup,args=(self.root,self.config,stopping))
            lane=threading.Thread(target=run_submission,args=(self.root,self.config,stopping))
            mail.start()
            try:
                self.assertTrue(entered.wait(2));lane.start()
                deadline=time.monotonic()+2
                while not (self.root/'state/demo-bookings.json').exists() and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue((self.root/'state/demo-bookings.json').exists())
            finally:
                stopping[0]=True;release.set();mail.join(5)
                if lane.ident:lane.join(5)
        self.assertFalse(mail.is_alive());self.assertFalse(lane.is_alive())

    def test_blocked_preparation_does_not_block_opening(self):
        import threading
        raw=dict(self.raw,signup_start_time=int(time.time())+4)
        secure_json(self.root/'state/demo-events.json',[raw]);self.approve(self.item(raw))
        entered=threading.Event();release=threading.Event();stopping=[False]
        original=OpeningClient.preflight
        def preflight(client,identity):
            if threading.current_thread().name.startswith('signup-prepare'):
                entered.set();release.wait(8)
            return original(client,identity)
        with patch.object(OpeningClient,'preflight',preflight):
            lane=threading.Thread(target=run_submission,args=(self.root,self.config,stopping));lane.start()
            try:
                self.assertTrue(entered.wait(2))
                deadline=time.monotonic()+6
                while not (self.root/'state/demo-bookings.json').exists() and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue((self.root/'state/demo-bookings.json').exists())
            finally:stopping[0]=True;release.set();lane.join(5)
        self.assertFalse(lane.is_alive())

    def test_offline_demo_full_workflow(self):
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream):demo(self.root)
        result=json.loads(stream.getvalue())
        self.assertEqual(result['state'],'success');self.assertEqual(result['email_previews'],3)
        self.assertEqual(result['real_network_requests'],0)

    def test_json_adapter_readonly_even_when_flags_enabled(self):
        secure_json(self.root/'examples/events.json',[self.raw])
        source=Source(self.root,replace(self.config,adapter='json',allow_live_submissions=True))
        with self.assertRaisesRegex(SourceError,'readonly'):source.submit({})

    def test_explicit_live_gate(self):
        source=Source(self.root,replace(self.config,adapter='json'))
        with self.assertRaisesRegex(SourceError,'not_explicitly'):source.submit({})

    def test_expired_adapter_deadline_rejected(self):
        self.source._scan_deadline=time.monotonic()-1
        with self.assertRaisesRegex(SourceError,'deadline'):self.source.profile()

    def test_preparation_does_not_submit_or_cache_quota(self):
        row=self.approve();client=OpeningClient(self.root,self.config)
        try:
            self.assertEqual(readiness(client,row),'ready')
            raw=copy.deepcopy(self.raw);raw['signup_quota']=20
            secure_json(self.root/'state/demo-events.json',[raw])
            self.assertFalse(client.preflight('1001')[1]['has_space'])
            self.assertFalse((self.root/'state/demo-bookings.json').exists())
        finally:client.close()

    def test_parallel_preflight_uses_independent_readers(self):
        import threading
        client=OpeningClient(self.root,self.config);barrier=threading.Barrier(3,timeout=3)
        def read(value):
            barrier.wait();return value
        try:
            with patch.object(client.profile_reader,'profile',side_effect=lambda:read(self.client.profile())), patch.object(client.detail_reader,'raw_detail',side_effect=lambda identity:read(copy.deepcopy(self.raw))), patch.object(client.schedule_reader,'registered',side_effect=lambda:read([])):
                self.assertEqual(client.preflight('1001')[1]['id'],'1001')
        finally:client.close()

    def test_stopped_opening_client_cannot_submit(self):
        client=OpeningClient(self.root,self.config)
        try:
            profile,item,_=client.preflight('1001');client.stopping=[True]
            with self.assertRaisesRegex(SourceError,'stopping'):client.submit(item['options'][0],profile)
        finally:client.close()

    def test_revoked_config_cannot_submit(self):
        client=OpeningClient(self.root,self.config)
        try:
            profile,item,_=client.preflight('1001');replace(self.config,signup_enabled=False).save(self.root)
            with self.assertRaisesRegex(SourceError,'disabled'):client.submit(item['options'][0],profile)
        finally:client.close()

    def test_session_change_cannot_submit(self):
        client=OpeningClient(self.root,self.config)
        try:
            profile,item,_=client.preflight('1001')
            with patch.object(client.source,'token',return_value='changed-session'):
                with self.assertRaisesRegex(SourceError,'auth_changed'):client.submit(item['options'][0],profile)
        finally:client.close()


if __name__=='__main__':unittest.main()
