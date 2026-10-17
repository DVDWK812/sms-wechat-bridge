"""Real Windows process/Job Object/HTTP acceptance; GUI sender is mocked.

Temporary loopback configuration + temporary fingerprint DB only. No production
configuration/Secret, no GUI navigation, no WeChat send, no logoff/lock action.
"""
from __future__ import annotations
import http.client
import contextlib
import os
import json
from pathlib import Path
import socket
import subprocess
import sys
import sqlite3
import tempfile
import threading
import time
from urllib.parse import urlencode
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psutil
import bridge
from tray_control import WindowsBackend, Controller, FLAGS_OFF, atomic_json, read_json


def wait_until(test, timeout=8):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        if test(): return
        time.sleep(.05)
    raise AssertionError('isolated_process_acceptance_timeout')


def run():
    results = {}
    with tempfile.TemporaryDirectory(prefix='sms-tray-mock-') as folder:
        root = Path(folder)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        config = {**FLAGS_OFF,'WECHAT_TARGET':'测试聊天','WECHAT_TARGET_TYPE':'chat',
            'SMS_BIND':'127.0.0.1','SMS_PORT':str(port),'SMS_ALLOWED_CLIENT_IPS':'127.0.0.1',
            'SMS_ALLOWED_SENDERS':'00000000001','SMS_ALLOW_ALL_SENDERS':'0'}
        atomic_json(root/'config.json',config)
        backend = WindowsBackend(root, mock=True)
        c = Controller(root, backend=backend, health=lambda _: '', proof=lambda *_:True)
        secret = 'tray-mock-only-'+'x'*40
        def form(content='【无效全文模拟托盘测试】验证码：略略略\n后续正文原样保留。', **changes):
            stamp=str(round(time.time()*1000))
            fields={'from':'00000000001','content':content,'timestamp':stamp,'sign':bridge.sign_for(stamp,secret)}
            fields.update(changes)
            return urlencode(fields)
        def post(body):
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=8)
            try:
                conn.request('POST','/sms',body.encode(),{'Content-Type':'application/x-www-form-urlencoded'})
                reply=conn.getresponse();return reply.status,reply.read().decode().strip()
            finally:conn.close()
        def ready():
            c.tick()
            return c.status=='running'
        def no_listener():
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.1):return False
            except OSError:return True
        try:
            assert no_listener()
            c.enable(human_confirmed=True);wait_until(ready)
            results['on_listener_and_queue_healthy']=True
            original=form()
            assert post(original)==(200,'sent')
            assert post(original)==(200,'duplicate')
            assert post(form(sign='bad'))==(403,'invalid signature')
            assert post(form(**{'from':'00000000002'}))==(403,'sender rejected')
            results['isolated_signed_http_mock_send_dedup_rejections']=True
            wait_until(lambda:backend.snapshot().get('last_result')=='sender rejected')
            c.tick()
            assert c.last['last_result']=='sender rejected'
            tree=psutil.Process(backend.process.pid)
            children=tree.children(recursive=True)
            c.disable();assert no_listener()
            assert all(not process.is_running() for process in [tree,*children])
            results['off_closes_listener_and_owned_process_tree']=True
            for _ in range(5):
                c.enable(human_confirmed=True);wait_until(ready)
                pid=backend.process.pid
                c.enable(human_confirmed=True)
                assert backend.process.pid==pid
                c.disable();assert no_listener()
            results['five_on_off_cycles_without_duplicate_receivers']=True
            c.enable(human_confirmed=True);wait_until(ready)
            backend.process.kill();backend.process.wait(3)
            c.tick();assert c.status=='fault'
            c.retry_at=0;wait_until(ready)
            assert post(original)==(200,'duplicate')
            results['receiver_crash_recovered_persistent_dedup']=True
            c.disable()
            # Crash after durable reservation, before mock sender returns.
            backend.mock_delay=3
            c.enable(human_confirmed=True);wait_until(ready)
            pending=form('【无效崩溃模拟】验证码：000001')
            outcome=[]
            def request():
                try:outcome.append(post(pending))
                except Exception:outcome.append('connection_ended')
            request_thread=threading.Thread(target=request);request_thread.start()
            wait_until(lambda:backend.snapshot().get('active'))
            backend.process.kill();backend.process.wait(3)
            c.tick();assert c.hard_fault and c.status=='fault'
            backend.mock_delay=0
            # An uncertain crash cannot automatically restore sending.
            c.disable();c.enable(human_confirmed=True);wait_until(ready)
            request_thread.join(4)
            assert post(pending)[0]==409
            assert not backend.snapshot().get('active')
            results['uncertain_crash_pauses_recovery_and_manual_restart_never_replays']=True
            # Revoke while mock send active, before it can complete.
            backend.mock_delay=2
            c.disable();c.enable(human_confirmed=True);wait_until(ready)
            pending=form('【无效关闭模拟】验证码：000002')
            request_thread=threading.Thread(target=request);request_thread.start()
            wait_until(lambda:backend.snapshot().get('active'))
            c.disable();request_thread.join(5)
            assert no_listener()
            with contextlib.closing(sqlite3.connect(root/'mock-fingerprints.sqlite3')) as db:
                assert db.execute('SELECT count(*) FROM sent').fetchone()[0]==1
                assert db.execute('SELECT count(*) FROM uncertain').fetchone()[0]==2
            results['off_during_active_mock_never_marks_sent']=True
            state=read_json(root/'config.json')
            assert all(state[key]==value for key,value in FLAGS_OFF.items())
            assert '00000' not in (root/'tray-status.json').read_text()
            results['isolated_flags_restored_no_sms_status_log']=True
            # Kill a separate real job owner; Windows must remove its entire
            # receiver tree even when Python finally blocks cannot run.
            owner=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--job-owner',str(root)],
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                wait_until(lambda:(root/'owner-ready.json').exists())
                details=read_json(root/'owner-ready.json')
                processes=[psutil.Process(pid) for pid in details['owned_pids']]
                psutil.Process(details['owner_pid']).kill()
                owner.wait(4)
                wait_until(lambda:all(not process.is_running() for process in processes))
                assert no_listener()
                results['job_owner_crash_kills_all_owned_children']=True
            finally:
                if owner.poll() is None:
                    owner.kill();owner.wait(3)
        finally:
            c.disable()
    return {'tests':results,'passed':all(results.values()),'actual_wechat_sends':0,'production_config_modified':False}

if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--job-owner':
        root=Path(sys.argv[2])
        backend=WindowsBackend(root,mock=True)
        try:
            backend.start(read_json(root/'config.json'))
            wait_until(lambda:backend.snapshot().get('queue_alive'))
            worker=psutil.Process(backend.process.pid)
            atomic_json(root/'owner-ready.json',{'owner_pid':os.getpid(),
                'owned_pids':[p.pid for p in [worker,*worker.children(recursive=True)]]})
            while True:time.sleep(1)
        finally:backend.stop()
        sys.exit(0)
    try:
        result=run()
        atomic_json(Path(__file__).resolve().parents[1]/'.local/native-acceptance.json',result)
        print(json.dumps(result,ensure_ascii=True))
    except Exception:
        # Fixed failure summary; no secret or request body in output.
        print(json.dumps({'passed':False,'error':'isolated_tray_acceptance_failed'}))
        raise
