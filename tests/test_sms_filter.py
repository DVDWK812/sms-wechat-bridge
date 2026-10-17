import contextlib
import http.client
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import bridge
import sms_filter as filters
import tray_control as control
from test_tray_control import FakeBackend
from wechat_sender import SendResult


class FilterTests(unittest.TestCase):
    def test_missing_config_uses_original_contains_verification_keyword(self):
        self.assertEqual(filters.filter_settings({}),('contains','验证码'))
        self.assertTrue(filters.matches_sms('验证码：略略略',*filters.filter_settings({})))
        self.assertFalse(filters.matches_sms('普通短信',*filters.filter_settings({})))

    def test_modes_match_literal_text_and_preserve_full_body(self):
        for mode,key,content,expected in (
            ('contains','动态口令','前文\n动态口令：无效模拟\n后文',True),
            ('contains','动态口令','验证码：无效模拟',False),
            ('not_contains','广告','普通短信全文',True),
            ('not_contains','广告','这条含广告字样',False),
            ('all','','普通短信全文',True),('all','未使用','验证码：无效模拟',True)):
            with self.subTest(mode=mode),patch.multiple(bridge,FILTER_MODE=mode,FILTER_KEYWORD=key):
                self.assertEqual(bridge.forwarding_text(content),content if expected else None)

    def test_keyword_is_literal_case_sensitive_and_spaces_are_not_normalized(self):
        self.assertTrue(filters.matches_sms('原文 A.* B 原文','contains',' A.* B '))
        self.assertFalse(filters.matches_sms('原文 A123 B 原文','contains','A.* B'))
        self.assertFalse(filters.matches_sms('otp: abc','contains','OTP'))

    def test_invalid_modes_and_empty_restricted_keyword_fail_closed(self):
        for mode,key in (('','x'),('unknown','x'),(None,'x'),('contains',''),
                         ('not_contains','  '),('contains','a\nb'),('contains','x'*129),('all',None)):
            with self.assertRaises(ValueError):filters.validate_filter(mode,key)

    def test_empty_sms_is_ignored_even_without_keyword_limit(self):
        with patch.multiple(bridge,FILTER_MODE='all',FILTER_KEYWORD=''):
            self.assertIsNone(bridge.forwarding_text(''))

    def test_status_description_clearly_identifies_all_three_modes(self):
        for mode,key,phrase in (('contains','测试','正文包含'),('not_contains','测试','正文不包含'),('all','','全部非空短信全文')):
            self.assertIn(phrase,filters.describe_filter({'SMS_FILTER_MODE':mode,'SMS_FILTER_KEYWORD':key}))


class FilterControllerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.config={**control.FLAGS_OFF,'WECHAT_TARGET':'示例群聊','WECHAT_TARGET_TYPE':'chat',
                     'SMS_BIND':'100.64.0.2','SMS_PORT':'8765','SMS_ALLOWED_CLIENT_IPS':'100.64.0.3',
                     'SMS_ALLOW_ALL_SENDERS':'1','SecretProtected':'opaque-test-only'}
        control.atomic_json(self.root/'config.json',self.config)
        self.backend=FakeBackend()
        self.c=control.Controller(self.root,backend=self.backend,health=lambda _: '',proof=lambda *_:True)

    def test_save_stops_running_worker_and_preserves_recipient_identity_and_private_config(self):
        self.c.verify_recipient(Mock(return_value=SendResult(True,'complete',verified=True,dry_run=True)))
        proof=(self.root/'tray-recipient-proof.json').read_bytes()
        self.c.enable(human_confirmed=True);self.c.tick()
        self.c.set_sms_filter('all','')
        saved=control.read_json(self.root/'config.json')
        self.assertTrue(self.backend.revoked.is_set());self.assertIsNone(self.backend.process)
        self.assertEqual((self.root/'tray-recipient-proof.json').read_bytes(),proof)
        for key in ('WECHAT_TARGET','WECHAT_TARGET_TYPE','SMS_ALLOWED_CLIENT_IPS','SecretProtected','SMS_ALLOW_ALL_SENDERS'):
            self.assertEqual(saved[key],self.config[key])
        self.assertTrue(all(saved[k]==v for k,v in control.FLAGS_OFF.items()))
        self.assertFalse(self.c.state['desired_enabled']);self.assertEqual(self.c.state['approved_scope'],'')
        self.assertEqual(filters.filter_settings(saved),('all',''))
        with self.assertRaises(PermissionError):self.c.enable()

    def test_invalid_save_does_not_change_running_rule_or_flags(self):
        self.c.enable(human_confirmed=True);self.c.tick()
        before=control.read_json(self.root/'config.json')
        with self.assertRaises(ValueError):self.c.set_sms_filter('not_contains','')
        self.assertEqual(control.read_json(self.root/'config.json'),before)
        self.assertIsNotNone(self.backend.process)

    def test_external_filter_change_invalidates_running_approval(self):
        for field,value in (('SMS_FILTER_MODE','all'),('SMS_FILTER_KEYWORD','另一个关键字')):
            self.c.disable();self.c.enable(human_confirmed=True);self.c.tick()
            changed=control.read_json(self.root/'config.json');changed[field]=value
            control.atomic_json(self.root/'config.json',changed)
            self.c.tick()
            self.assertEqual(self.c.status,'fault');self.assertIsNone(self.backend.process)
            self.assertEqual(self.c.error,'authorization_or_target_changed')

    def test_saved_filter_survives_restart_without_auto_enable_and_defaults_are_canonical(self):
        self.assertEqual(control.scope(self.config),control.scope({**self.config,'SMS_FILTER_MODE':'contains','SMS_FILTER_KEYWORD':'验证码'}))
        self.c.set_sms_filter('not_contains','广告')
        restarted=control.Controller(self.root,backend=FakeBackend(),health=lambda _: '',proof=lambda *_:True)
        restarted.tick()
        self.assertEqual(restarted.status,'off')
        self.assertEqual(filters.filter_settings(control.read_json(restarted.config_path)),('not_contains','广告'))

    def test_invalid_filter_cannot_write_production_flags_when_enable_is_confirmed(self):
        invalid={**self.config,'SMS_FILTER_MODE':'not_contains','SMS_FILTER_KEYWORD':''}
        control.atomic_json(self.root/'config.json',invalid)
        with self.assertRaises(ValueError):self.c.enable(human_confirmed=True)
        self.assertEqual(control.read_json(self.root/'config.json'),invalid)
        self.assertEqual(self.backend.starts,0)


class FilterProcessTests(unittest.TestCase):
    def test_each_mode_reaches_real_managed_receiver_via_environment_and_signed_http(self):
        cases=(('contains','动态','【无效模拟】动态口令：略略略','普通短信'),
               ('not_contains','拦截','【无效模拟】普通短信全文','这条含拦截字样'),
               ('all','','【无效模拟】普通短信，无默认关键字',None))
        for mode,key,accepted,rejected in cases:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                config={**control.FLAGS_OFF,'SMS_BIND':'127.0.0.1','SMS_PORT':str(port),
                        'SMS_ALLOWED_CLIENT_IPS':'127.0.0.1','SMS_ALLOW_ALL_SENDERS':'1',
                        'WECHAT_TARGET':'临时测试聊天','WECHAT_TARGET_TYPE':'chat',
                        'SMS_FILTER_MODE':mode,'SMS_FILTER_KEYWORD':key}
                backend=control.WindowsBackend(root,mock=True);backend.start(config)
                def post(body,wrong_signature=False):
                    stamp=str(round(time.time()*1000));secret='tray-mock-only-'+'x'*40
                    data=urlencode({'from':'00000000001','content':body,'timestamp':stamp,
                                    'sign':'bad' if wrong_signature else bridge.sign_for(stamp,secret)})
                    with contextlib.closing(http.client.HTTPConnection('127.0.0.1',port,timeout=2)) as conn:
                        conn.request('POST','/sms',data.encode(),{'Content-Type':'application/x-www-form-urlencoded'})
                        response=conn.getresponse();return response.status,response.read().decode().strip()
                try:
                    deadline=time.monotonic()+5
                    while not backend.snapshot().get('queue_alive'):
                        self.assertLess(time.monotonic(),deadline);time.sleep(.02)
                    if rejected is not None:self.assertEqual(post(rejected),(200,'ignored'))
                    self.assertEqual(post(accepted,wrong_signature=True),(403,'invalid signature'))
                    self.assertEqual(post(accepted),(200,'sent'))
                    self.assertEqual(post(accepted),(200,'duplicate'))
                finally:backend.stop()


if __name__=='__main__':unittest.main()
