import json
import hashlib
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import tray_control as control
import bridge
from wechat_sender import SendResult, NativeDesktop, SenderError


class FakeBackend:
    def __init__(self):
        self.process = None
        self.starts = self.stops = 0
        self.value = {}
        self.revoked = threading.Event()

    def permit(self):
        self.revoked.clear()

    def revoke(self):
        self.revoked.set()

    def start(self, config):
        if self.process:
            raise AssertionError('duplicate start')
        self.starts += 1
        self.process = SimpleNamespace(pid=self.starts)
        self.value = {'queue_alive': True, 'halted': False}

    def snapshot(self):
        return self.value

    def stop(self):
        self.stops += 1
        self.process = None
        self.value = {}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {**control.FLAGS_OFF, 'WECHAT_TARGET': '测试好友', 'WECHAT_TARGET_TYPE': 'friend',
                       'SMS_BIND': '100.64.0.2', 'SMS_ALLOWED_CLIENT_IPS': '100.64.0.3', 'SMS_PORT': '8765',
                       'SMS_ALLOWED_SENDERS': '00000000001', 'SMS_ALLOW_ALL_SENDERS': '1',
                       'SecretProtected': 'opaque-test-only-not-a-secret', 'AcceptanceComplete': False}
        control.atomic_json(self.root/'config.json', self.config)
        self.backend = FakeBackend()
        self.health = Mock(return_value='')
        self.proof = Mock(return_value=True)
        self.c = control.Controller(self.root, backend=self.backend, health=self.health, proof=self.proof)

    def enable(self):
        self.c.enable(human_confirmed=True)
        self.c.tick()

    def test_first_off_does_not_change_config_or_start(self):
        self.c.tick()
        self.assertEqual(self.c.status, 'off')
        self.assertEqual(self.backend.starts, 0)
        self.assertEqual(control.read_json(self.root/'config.json'), self.config)

    def test_no_confirmation_never_grants_flags(self):
        for value in (False, None, 'false', '0', 1):
            with self.assertRaises(PermissionError):
                self.c.enable(human_confirmed=value)
        self.assertEqual(control.read_json(self.root/'config.json'), self.config)

    def test_repeated_switches_one_receiver_flags_off_after_close_no_secret_copy(self):
        for _ in range(10):
            self.enable()
            self.c.enable(human_confirmed=True)
            self.assertEqual(self.c.status, 'running')
            self.c.disable()
            self.assertEqual(self.c.status, 'off')
        self.assertEqual(self.backend.starts, 10)
        saved = control.read_json(self.root/'config.json')
        self.assertTrue(all(saved[key] == value for key,value in control.FLAGS_OFF.items()))
        self.assertEqual(saved['SecretProtected'], self.config['SecretProtected'])
        self.assertFalse(saved['AcceptanceComplete'])
        self.assertNotIn('SecretProtected', (self.root/'tray-settings.json').read_text())

    def test_stop_revokes_before_waiting_for_supervisor_lock(self):
        self.enable()
        with self.c.lock:
            other = threading.Thread(target=self.c.disable)
            other.start()
            self.assertTrue(self.backend.revoked.wait(1))
        other.join(1)
        self.assertFalse(other.is_alive())

    def test_resume_only_prior_explicit_scope_and_flags(self):
        self.enable()
        second = FakeBackend()
        restored = control.Controller(self.root, backend=second, health=self.health, proof=self.proof)
        restored.tick()
        self.assertEqual(second.starts, 1)
        self.config.update(control.FLAGS_ON, WECHAT_TARGET='不同好友')
        control.atomic_json(self.root/'config.json', self.config)
        restored.tick()
        self.assertEqual(restored.status, 'fault')
        self.assertIsNone(second.process)
        self.assertEqual(second.starts, 1)

    def test_persisted_on_cannot_override_production_flags_off(self):
        control.atomic_json(self.root/'tray-settings.json', {'desired_enabled': True,
            'approved_scope': control.scope(self.config), 'human_approved_at': time.time()})
        restored = control.Controller(self.root, backend=self.backend, health=self.health, proof=self.proof)
        restored.tick()
        self.assertEqual(restored.status, 'fault')
        self.assertEqual(self.backend.starts, 0)

    def test_prior_digit_only_policy_authorization_cannot_automatically_resume_full_text(self):
        config={**self.config,**control.FLAGS_ON}
        control.atomic_json(self.root/'config.json',config)
        old_scope={key:config.get(key,'') for key in control.SCOPE_FIELDS}
        old_scope['client_version']=control.VERSION
        previous=hashlib.sha256(json.dumps(old_scope,sort_keys=True,ensure_ascii=True).encode()).hexdigest()
        control.atomic_json(self.root/'tray-settings.json',{'desired_enabled':True,
            'approved_scope':previous,'human_approved_at':time.time()})
        restored=control.Controller(self.root,backend=self.backend,health=self.health,proof=self.proof)
        restored.tick()
        self.assertEqual(restored.status,'fault')
        self.assertEqual(restored.error,'authorization_or_target_changed')
        self.assertEqual(self.backend.starts,0)

    def test_failed_enable_remains_fault_until_closed_without_granting_flags(self):
        self.health.return_value='tailscale_not_running'
        with self.assertRaises(RuntimeError):self.enable()
        self.c.tick()
        self.assertEqual(self.c.status,'fault')
        self.assertEqual(control.read_json(self.root/'config.json'),self.config)
        self.c.disable();self.c.tick()
        self.assertEqual(self.c.status,'off')

    def test_tray_restart_with_uncertain_records_cannot_automatically_resume(self):
        self.enable()
        second=FakeBackend();second.has_uncertain=lambda:True
        restored=control.Controller(self.root,backend=second,health=self.health,proof=self.proof)
        restored.tick()
        self.assertEqual(restored.status,'fault')
        self.assertTrue(restored.hard_fault)
        self.assertEqual(second.starts,0)

    def test_invalid_bool_setting_fails_closed(self):
        control.atomic_json(self.root/'tray-settings.json', {'desired_enabled':'false'})
        restored = control.Controller(self.root, backend=self.backend, health=self.health, proof=self.proof)
        restored.tick()
        self.assertFalse(restored.state['desired_enabled'])
        self.assertEqual(self.backend.starts, 0)

    def test_missing_proof_or_health_failure_never_changes_flags(self):
        for proof,health in [(False,''),(True,'tailscale_not_running'),(True,'desktop_locked_or_secure_prompt')]:
            self.proof.return_value, self.health.return_value = proof,health
            with self.assertRaises(RuntimeError):
                self.c.enable(human_confirmed=True)
            self.assertEqual(control.read_json(self.root/'config.json'), self.config)

    def test_unavailable_dependencies_fault_and_resume_only_new_work(self):
        self.enable()
        for problem in ('tailscale_not_running','paired_phone_offline','logged_in_main_window_not_unique',
                        'desktop_locked_or_secure_prompt','active_console_session_required'):
            self.health.return_value = problem
            self.c.tick()
            self.assertEqual(self.c.status, 'fault')
            self.assertIsNone(self.backend.process)
            self.health.return_value = ''
            self.c.tick(); self.c.tick()
            self.assertEqual(self.c.status, 'running')

    def test_sender_unknown_halts_until_explicit_manual_cycle(self):
        self.enable()
        self.backend.value['halted'] = True
        self.c.tick()
        for _ in range(4):
            self.c.tick()
        self.assertEqual(self.backend.starts, 1)
        self.assertTrue(self.c.hard_fault)
        self.c.disable(); self.enable()
        self.assertEqual(self.backend.starts, 2)

    def test_receiver_crash_bounded_recovery_and_no_message_replay(self):
        self.enable()
        for index in range(3):
            self.backend.value = {'dead': True}
            self.c.tick()
            self.assertEqual(self.c.status,'fault')
            if index < 2:
                self.c.retry_at = 0
                self.c.tick()
        self.assertTrue(self.c.hard_fault)
        for _ in range(4): self.c.tick()
        self.assertEqual(self.backend.starts, 3)

    def test_queue_thread_dead_and_heartbeat_stale_are_detected(self):
        self.enable()
        self.backend.value = {'queue_alive': False}
        self.c.tick()
        self.assertTrue(self.c.hard_fault)
        self.c.disable(); self.enable()
        self.backend.value = {'stale': True}
        self.c.tick()
        self.assertEqual(self.c.status,'fault')
        self.assertIsNone(self.backend.process)

    def test_uncertain_crash_never_recovers_automatically(self):
        self.enable()
        self.backend.has_uncertain = lambda: True
        self.backend.value = {'dead':True}
        self.c.tick()
        self.c.retry_at=0
        for _ in range(4):self.c.tick()
        self.assertTrue(self.c.hard_fault)
        self.assertEqual(self.c.error,'incomplete_request_manual_review')
        self.assertEqual(self.backend.starts,1)

    def test_disable_with_bad_config_still_terminates_process(self):
        self.enable()
        (self.root/'config.json').write_text('invalid-json')
        with self.assertRaises(Exception): self.c.disable()
        self.assertTrue(self.backend.revoked.is_set())
        self.assertIsNone(self.backend.process)
        self.assertFalse(control.read_json(self.root/'tray-settings.json')['desired_enabled'])

    def test_status_only_non_sensitive_and_retained_after_relaunch(self):
        self.enable()
        self.backend.value.update(last_time=time.time(), last_result='sent', last_stage='complete')
        self.c.tick()
        state = control.read_json(self.root/'tray-status.json')
        self.assertEqual(state['last_result'],'sent')
        self.assertNotIn('SecretProtected', json.dumps(state))
        second = control.Controller(self.root,backend=FakeBackend(),health=self.health,proof=self.proof)
        self.assertEqual(second.last['last_result'],'sent')


class QueueSwitchTests(unittest.TestCase):
    def test_off_discards_waiting_plaintext_and_never_calls_gui(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(bridge,'DB_PATH',str(Path(folder)/'test.sqlite3')):
            server = bridge.BridgeServer(('127.0.0.1',0), bridge.Handler)
            server.stopping.set()
            server.send_thread.join(1)
            server.sender.send = Mock()
            jobs = [bridge.SendJob('验证码：000000',time.time()+30) for _ in range(3)]
            for job in jobs: server.jobs.put(job)
            jobs[0].future.cancel()
            server.stop_forwarding()
            self.assertEqual(server.jobs.unfinished_tasks,0)
            self.assertTrue(all(job.message == '' for job in jobs))
            self.assertFalse(jobs[1].future.result().success)
            server.sender.send.assert_not_called()
            server.server_close()

    def test_disabled_lease_blocks_valid_message_before_reservation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(bridge,'DB_PATH',str(Path(folder)/'test.sqlite3')):
            server = bridge.BridgeServer(('127.0.0.1',0),bridge.Handler)
            server.send_permitted = lambda: False
            server.sender.send = Mock()
            self.assertEqual(server.handle_verified_sms('00000000001','验证码：000000'),'uncertain')
            self.assertEqual(server.db.execute('SELECT count(*) FROM uncertain').fetchone()[0],0)
            server.sender.send.assert_not_called()
            server.server_close()

    def test_gui_revoked_event_stops_before_desktop_or_keyboard(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateEventW.restype = wintypes.HANDLE
        name = 'Local\\SmsWechatBridge-Test-' + str(time.time_ns())
        handle = kernel.CreateEventW(None,True,False,name)
        self.assertTrue(handle)
        try:
            desktop = object.__new__(NativeDesktop)
            desktop.api = Mock(); desktop.u32 = Mock()
            with patch.dict('os.environ',{'BRIDGE_TRAY_SEND_EVENT':name}):
                with self.assertRaises(SenderError) as caught:
                    desktop.guard_desktop()
            self.assertEqual(caught.exception.error,'forwarding_switched_off')
            desktop.api.keybd_event.assert_not_called()
            desktop.u32.OpenInputDesktop.assert_not_called()
        finally:
            kernel.CloseHandle(wintypes.HANDLE(handle))

if __name__ == '__main__':
    unittest.main()
