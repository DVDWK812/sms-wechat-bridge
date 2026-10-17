import json
import socket
import time
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import tray_control as control
from test_tray_control import FakeBackend
from wechat_sender import SendResult


class RecipientControlTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = {**control.FLAGS_OFF, 'WECHAT_TARGET':'原好友', 'WECHAT_TARGET_TYPE':'friend',
                       'SMS_BIND':'100.64.0.2', 'SMS_PORT':'8765', 'SMS_ALLOWED_CLIENT_IPS':'100.64.0.3',
                       'SMS_ALLOW_ALL_SENDERS':'1', 'SecretProtected':'opaque-test-only'}
        control.atomic_json(self.root/'config.json', self.config)
        self.backend = FakeBackend()
        self.controller = control.Controller(self.root, backend=self.backend,
                                             health=Mock(return_value=''), proof=Mock(return_value=True))

    def success(self):
        return SendResult(True, 'complete', verified=True, dry_run=True)

    def test_change_running_target_revokes_live_gate_and_old_authorization(self):
        self.controller.enable(human_confirmed=True)
        self.controller.set_recipient('新好友')
        cfg=control.read_json(self.root/'config.json')
        self.assertEqual(cfg['WECHAT_TARGET'],'新好友')
        self.assertEqual(cfg['WECHAT_TARGET_TYPE'],'chat')
        self.assertTrue(all(cfg[k]==v for k,v in control.FLAGS_OFF.items()))
        self.assertEqual(cfg['SecretProtected'],self.config['SecretProtected'])
        self.assertEqual(cfg['SMS_ALLOWED_CLIENT_IPS'],self.config['SMS_ALLOWED_CLIENT_IPS'])
        self.assertEqual(cfg['SMS_ALLOW_ALL_SENDERS'],'1')
        self.assertTrue(self.backend.revoked.is_set())
        self.assertIsNone(self.backend.process)
        self.assertEqual(self.controller.state,{'desired_enabled':False,'approved_scope':'','human_approved_at':''})

    def test_invalid_names_never_modify_target_or_running_state(self):
        self.controller.enable(human_confirmed=True)
        before=control.read_json(self.root/'config.json')
        for name in ('',None,'a b',' 好友','好友\n','x'*65,'文件传输助手','a\x00b'):
            with self.subTest(name=name),self.assertRaises(ValueError):
                self.controller.set_recipient(name)
        self.assertEqual(control.read_json(self.root/'config.json'),before)
        self.assertIsNotNone(self.backend.process)

    def test_identity_only_proof_requires_reconfirmation_and_never_overwrites_send_evidence(self):
        old={'recipient':'原好友','client_version':control.VERSION,'success':True,'verified':True,'attempted':True}
        control.atomic_json(self.root/'ocr-acceptance-friend.json',old)
        self.controller.set_recipient('新好友')
        self.assertFalse(control.friend_proof(self.root,control.read_json(self.root/'config.json')))
        probe=Mock(return_value=self.success())
        self.controller.verify_recipient(probe)
        probe.assert_called_once_with('新好友')
        cfg=control.read_json(self.root/'config.json')
        self.assertTrue(control.friend_proof(self.root,cfg))
        self.assertEqual(control.read_json(self.root/'ocr-acceptance-friend.json'),old)
        self.assertTrue(all(cfg[k]==v for k,v in control.FLAGS_OFF.items()))
        self.assertFalse(self.controller.state['desired_enabled'])
        self.assertFalse(control.approved(cfg,self.controller.state))
        self.assertIsNone(self.backend.process)
        with self.assertRaises(PermissionError):self.controller.enable()

    def test_failed_or_malformed_probe_does_not_grant_identity(self):
        self.controller.set_recipient('新好友')
        for result in (SendResult(False,'identity','no_match',dry_run=True),
                       SendResult(True,'complete',attempted=True,verified=True,dry_run=True),
                       SendResult(True,'complete',verified=True,dry_run=False),
                       SendResult(True,'input',verified=True,dry_run=True),
                       SendResult(1,'complete',verified=True,dry_run=True)):
            with self.subTest(result=result),self.assertRaises(RuntimeError):
                self.controller.verify_recipient(Mock(return_value=result))
            self.assertFalse(control.friend_proof(self.root,control.read_json(self.root/'config.json')))
            self.assertFalse(self.controller.state['desired_enabled'])
            self.assertFalse(self.controller.verifying)

    def test_enable_during_verification_blocked_and_target_change_discards_stale_proof(self):
        self.controller.set_recipient('新好友')
        entered=threading.Event();resume=threading.Event();outcome=[]
        probe=Mock()
        def check(name):
            entered.set();resume.wait(2);return self.success()
        probe.side_effect=check
        def run():
            try:self.controller.verify_recipient(probe)
            except RuntimeError as error:outcome.append(str(error))
        worker=threading.Thread(target=run);worker.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(RuntimeError,'verification_in_progress'):
            self.controller.enable(human_confirmed=True)
        self.controller.set_recipient('另一个好友')
        resume.set();worker.join(2)
        self.assertFalse(worker.is_alive())
        probe.cancel.assert_called()
        self.assertEqual(outcome,['recipient_verification_cancelled'])
        cfg=control.read_json(self.root/'config.json')
        self.assertEqual(cfg['WECHAT_TARGET'],'另一个好友')
        self.assertFalse(control.friend_proof(self.root,cfg))

    def test_identity_proof_is_bound_to_exact_target_version_and_strict_boolean_schema(self):
        self.controller.set_recipient('新好友')
        self.controller.verify_recipient(Mock(return_value=self.success()))
        path=self.root/'tray-recipient-proof.json';original=control.read_json(path)
        cfg=control.read_json(self.root/'config.json')
        for change in ({'recipient':'新好友2'},{'client_version':'4.1.0'}, {'verified':1},
                       {'attempted':True},{'dry_run':False},{'identity_only':False},
                       {'schema':'other'},{'success':'true'},{'target_type':'group'},{'selection_policy':'other'}):
            control.atomic_json(path,{**original,**change})
            self.assertFalse(control.friend_proof(self.root,cfg),change)

    def test_real_verifier_only_passes_dry_identity_payload_and_owns_subprocess(self):
        fake_job=Mock();fake_process=Mock()
        fake_process.returncode=0;fake_process.poll.return_value=0
        fake_process.communicate.return_value=(json.dumps(self.success().__dict__),'')
        with patch('win32job.CreateJobObject',return_value=fake_job) as create_job, \
             patch('win32job.QueryInformationJobObject',return_value={'BasicLimitInformation':{'LimitFlags':0}}), \
             patch('win32job.SetInformationJobObject'),patch('win32job.AssignProcessToJobObject') as assign, \
             patch.object(control.subprocess,'Popen',return_value=fake_process) as popen:
            fake_process._handle=10
            result=control.RecipientVerifier(self.root)('新好友')
        self.assertTrue(result.success)
        payload=json.loads(fake_process.communicate.call_args_list[0].args[0])
        self.assertEqual(payload,{'recipient':'新好友','text':'联系人核验','dry_run':True,'approved':False,'target_type':'chat'})
        child_env=popen.call_args.kwargs['env']
        allowed={'SMS_WECHAT_DATA_DIR','SMS_OCR_MODEL_DIR'}
        self.assertFalse(any(key.startswith(('SMS_','WECHAT_','BRIDGE_TRAY_')) and key not in allowed for key in child_env))
        self.assertEqual(child_env['SMS_WECHAT_DATA_DIR'],str(self.root))
        assign.assert_called_once_with(fake_job,10)
        self.assertTrue(create_job.call_args.args[1].startswith('Local\\SmsWechatBridge-Identity-'))
        fake_job.Close.assert_called_once()

    def test_close_before_background_verification_starts_never_invokes_probe(self):
        self.controller.set_recipient('新好友')
        revision=self.controller.recipient_revision
        self.controller.disable()
        probe=Mock(return_value=self.success())
        with self.assertRaisesRegex(RuntimeError,'verification_cancelled'):
            self.controller.verify_recipient(probe,expected_revision=revision)
        probe.assert_not_called()

    def test_group_and_auto_chat_proofs_allow_only_manually_confirmed_enable(self):
        for kind in ('group','chat'):
            self.controller.set_recipient('示例群聊',target_type=kind)
            cfg=control.read_json(self.root/'config.json')
            self.assertEqual(cfg['WECHAT_TARGET_TYPE'],kind)
            self.assertFalse(control.friend_proof(self.root,cfg))
            self.controller.verify_recipient(Mock(return_value=self.success()))
            self.assertTrue(control.friend_proof(self.root,cfg))
            self.assertFalse(self.controller.state['desired_enabled'])
            with self.assertRaises(PermissionError):self.controller.enable()
            self.controller.proof=control.friend_proof
            self.controller.enable(human_confirmed=True)
            self.controller.tick()
            self.assertEqual(self.controller.status,'running')
            self.controller.disable()

    def test_saving_same_verified_chat_preserves_proof_and_manual_enable_gate(self):
        self.controller.set_recipient('示例群聊')
        self.controller.verify_recipient(Mock(return_value=self.success()))
        proof_path=self.root/'tray-recipient-proof.json'
        original=proof_path.read_bytes()
        for _ in range(3):
            self.assertIs(self.controller.set_recipient('示例群聊'),True)
            self.assertEqual(proof_path.read_bytes(),original)
            self.assertFalse(self.controller.state['desired_enabled'])
            self.assertTrue(control.friend_proof(self.root,control.read_json(self.root/'config.json')))
        self.controller.proof=control.friend_proof
        self.controller.enable(human_confirmed=True)
        self.controller.tick()
        self.assertEqual(self.controller.status,'running')
        self.controller.disable()

    def test_changed_name_or_type_invalidates_previous_verified_chat(self):
        for name,kind in (('另一个群','chat'),('示例群聊','group')):
            self.controller.set_recipient('示例群聊')
            self.controller.verify_recipient(Mock(return_value=self.success()))
            self.assertIs(self.controller.set_recipient(name,target_type=kind),False)
            self.assertFalse(control.friend_proof(self.root,control.read_json(self.root/'config.json')))
            self.assertFalse(self.controller.state['desired_enabled'])

    def test_repeated_save_never_promotes_pending_or_mismatched_policy_proof(self):
        self.controller.set_recipient('示例群聊')
        self.assertIs(self.controller.set_recipient('示例群聊'),False)
        self.controller.verify_recipient(Mock(return_value=self.success()))
        proof=control.read_json(self.root/'tray-recipient-proof.json')
        proof['selection_policy']='old_unverified_policy'
        control.atomic_json(self.root/'tray-recipient-proof.json',proof)
        self.assertIs(self.controller.set_recipient('示例群聊'),False)
        self.assertFalse(control.friend_proof(self.root,control.read_json(self.root/'config.json')))


class AtomicStatusTests(unittest.TestCase):
    def test_transient_windows_sharing_violation_retries_but_other_errors_propagate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'status.json'
            error=PermissionError('isolated-lock');error.winerror=32
            real_replace=control.os.replace
            count=0
            def replace(src,dst):
                nonlocal count
                count+=1
                if count==1:raise error
                return real_replace(src,dst)
            with patch.object(control.os,'replace',side_effect=replace):
                control.atomic_json(path,{'status':'ok'})
            self.assertEqual(count,2)
            self.assertEqual(control.read_json(path),{'status':'ok'})
            with patch.object(control.os,'replace',side_effect=OSError('not-a-transient-lock')):
                with self.assertRaises(OSError):control.atomic_json(path,{'status':'bad'})
            self.assertEqual(control.read_json(path),{'status':'ok'})
            self.assertFalse(list(Path(tmp).glob('*.tmp')))

    def test_real_windows_worker_survives_status_reader_lock_and_keeps_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            config={**control.FLAGS_OFF,'SMS_BIND':'127.0.0.1','SMS_ALLOWED_CLIENT_IPS':'127.0.0.1',
                    'SMS_PORT':str(port),'SMS_ALLOW_ALL_SENDERS':'1',
                    'WECHAT_TARGET':'isolated-test','WECHAT_TARGET_TYPE':'friend'}
            backend=control.WindowsBackend(root,mock=True)
            backend.start(config)
            try:
                deadline=time.monotonic()+5
                while not backend.snapshot().get('queue_alive'):
                    self.assertLess(time.monotonic(),deadline)
                    time.sleep(.02)
                before=backend.snapshot()['heartbeat']
                with backend.status_path.open('rb'):
                    time.sleep(.65)
                self.assertIsNone(backend.process.poll())
                deadline=time.monotonic()+3
                while backend.snapshot().get('heartbeat',0)<=before:
                    self.assertLess(time.monotonic(),deadline)
                    time.sleep(.02)
                self.assertTrue(backend.snapshot()['queue_alive'])
            finally:
                backend.stop()


if __name__=='__main__':unittest.main()
