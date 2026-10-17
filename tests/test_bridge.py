import base64
import hashlib
import hmac
import contextlib
import http.client
import io
import os
import queue
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote, urlencode

import bridge
from wechat_sender import SendResult


class BridgeTests(unittest.TestCase):
    def test_legacy_fixed_acceptance_code_validation(self):
        self.assertEqual(bridge.extract_otp('【示例】您的验证码为 123456，5分钟有效'), '123456')
        self.assertEqual(bridge.extract_otp('123456 是您本次登录的验证码'), '123456')
        self.assertEqual(bridge.extract_otp('code: 384021'), '384021')
        self.assertIsNone(bridge.extract_otp('快递单号12345678'))
        self.assertEqual(bridge.extract_otp('验证码 123456 还有 321098'), '123456')
        self.assertIsNone(bridge.extract_otp('验证码：abc；订单号12345 和 654321'))
        self.assertIsNone(bridge.extract_otp('验证码 123456，另一验证码 654321'))
        self.assertIsNone(bridge.extract_otp('验证码：略略略'))

    def test_keyword_returns_complete_body_without_numeric_or_length_filter(self):
        for content in ('验证码：略略略','验证码：A1b','验证码：1','验证码：12345678901234567890',
                        '验证码','验证码 123456；验证码 654321','前文\n验证码：无效模拟\r\n  后文\t完',
                        '验证码：'+ '无效长短信'*100):
            self.assertEqual(bridge.forwarding_text(content),content)
        for content in ('','123456','快递单号12345678','校验码：123456','OTP: 123456'):
            self.assertIsNone(bridge.forwarding_text(content))

    def test_signature(self):
        secret = 'a' * 40
        ts = '1700000000000'
        expected = base64.b64encode(hmac.new(secret.encode(), f'{ts}\n{secret}'.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(bridge.sign_for(ts, secret), expected)
        self.assertTrue(bridge.valid_sign(ts, expected, secret))
        self.assertFalse(bridge.valid_sign(ts, 'bad', secret))
        self.assertFalse(bridge.valid_sign(ts, '非ASCII签名', secret))
        self.assertTrue(bridge.valid_sign(ts, quote(expected, safe=''), secret))

    def test_fingerprint(self):
        self.assertEqual(bridge.fingerprint('foo', '验证码 123456', 'a'*40), bridge.fingerprint('foo', '验证码 123456', 'a'*40))
        self.assertNotEqual(bridge.fingerprint('foo', '验证码 123456', 'a'*40), bridge.fingerprint('foo', '验证码 876543', 'a'*40))


class HTTPTests(unittest.TestCase):
    """Real loopback HTTP requests; every body is an invalid synthetic OTP."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / 'dedup.sqlite3')
        self.patches = patch.multiple(bridge, SECRET='test-only-' + 'x' * 40,
                                     SENDERS={'+8600000000000'}, ALLOW_ALL_SENDERS=False,TARGET='测试群',
                                     TARGET_TYPE='group', CLIENT_IPS={'127.0.0.1'},
                                     DRY_RUN=True, DB_PATH=self.db_path,
                                     REQUEST_TIMEOUT_SECONDS=0.15)
        self.patches.start()
        self.output = io.StringIO()
        self.out_patch = contextlib.redirect_stdout(self.output)
        self.err_patch = contextlib.redirect_stderr(self.output)
        self.out_patch.__enter__()
        self.err_patch.__enter__()
        self.start_server()

    def start_server(self):
        ready = threading.Event()
        def serve():
            # Production uses locked SQLite and a separate serialized UI worker.
            self.server = bridge.BridgeServer(('127.0.0.1', 0), bridge.Handler)
            self.port = self.server.server_address[1]
            ready.set()
            try:
                self.server.serve_forever(poll_interval=0.01)
            finally:
                self.server.server_close()
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(timeout=2))

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=2)

    def tearDown(self):
        self.stop_server()
        self.err_patch.__exit__(None, None, None)
        self.out_patch.__exit__(None, None, None)
        self.patches.stop()
        self.temp.cleanup()

    def form(self, **changes):
        stamp = str(round(time.time() * 1000))
        fields = {'from': '+8600000000000', 'content': '【无效模拟测试】验证码 000000',
                  'timestamp': stamp, 'sign': bridge.sign_for(stamp, bridge.SECRET)}
        fields.update(changes)
        return urlencode(fields)

    def post(self, body=None, path='/sms', content_type='application/x-www-form-urlencoded'):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            conn.request('POST', path, (body if body is not None else self.form()).encode(),
                         {'Content-Type': content_type})
            response = conn.getresponse()
            return response.status, response.read().decode().strip()
        finally:
            conn.close()

    def test_valid_synthetic_dry_run_and_no_plaintext_storage(self):
        self.assertEqual(self.post(), (200, 'dry_run'))
        self.assertNotIn('000000', self.output.getvalue())
        self.assertNotIn(bridge.SECRET, self.output.getvalue())
        self.assertNotIn(b'000000', Path(self.db_path).read_bytes())
        self.assertNotIn(bridge.SECRET.encode(), Path(self.db_path).read_bytes())

    def test_source_ip_rejected_without_trusting_forwarded_headers(self):
        with patch.object(bridge, 'CLIENT_IPS', {'100.64.0.7'}):
            self.assertEqual(self.post(), (403, 'source rejected'))

    def test_sender_exact_match_and_no_number_format_expansion(self):
        for value in ('+8600000000003', '00000000000', '+8600000000000 ', ''):
            with self.subTest(value=value):
                self.assertEqual(self.post(self.form(**{'from': value})), (403, 'sender rejected'))

    def test_invalid_signature(self):
        self.assertEqual(self.post(self.form(sign='bad')), (403, 'invalid signature'))
        self.assertIn('status=403; result=invalid signature', self.output.getvalue())
        self.assertNotIn('000000', self.output.getvalue())
        self.assertNotIn(bridge.SECRET, self.output.getvalue())

    def test_all_sender_mode_accepts_different_numbers_but_keeps_ip_and_hmac(self):
        with patch.object(bridge,'ALLOW_ALL_SENDERS',True):
            self.assertEqual(self.post(self.form(**{'from':'00000000004'})),(200,'dry_run'))
            self.assertEqual(self.post(self.form(**{'from':'another-number','sign':'bad'})),(403,'invalid signature'))
            with patch.object(bridge,'CLIENT_IPS',{'100.64.0.7'}):
                self.assertEqual(self.post(self.form(**{'from':'00000000004'})),(403,'source rejected'))

    def test_stale_and_future_timestamps(self):
        for offset in (-121, 121):
            stamp = str(round((time.time() + offset) * 1000))
            self.assertEqual(self.post(self.form(timestamp=stamp, sign=bridge.sign_for(stamp, bridge.SECRET))),
                             (403, 'expired'))

    def test_invalid_timestamp_and_missing_field(self):
        self.assertEqual(self.post(self.form(timestamp='wrong')), (400, 'invalid timestamp'))
        self.assertEqual(self.post('from=00000000000'), (400, 'invalid form'))

    def test_content_without_exact_keyword_ignored(self):
        self.server.sender.send=Mock()
        for content in ('快递单号12345678', '校验码 123456', 'OTP: abcdef', '普通消息'):
            self.assertEqual(self.post(self.form(content=content)), (200, 'ignored'))
        self.server.sender.send.assert_not_called()
        self.assertEqual(self.server.db.execute('SELECT count(*) FROM uncertain').fetchone()[0],0)

    def test_keyword_full_body_reaches_configured_contact_unchanged(self):
        result=SendResult(True,'complete',attempted=True,verified=True)
        bodies=('验证码：略略略','验证码：A1b','验证码：1','验证码：123456789012',
                '验证码 000000；验证码 000001',
                '  【无效全文模拟】\r\n验证码：略略略\n后文 & + % = 空格\t保留  ',
                '【无效长短信】验证码：'+ '仅作模拟；'*100)
        with patch.multiple(bridge,DRY_RUN=False,TARGET='托盘选择的测试好友',TARGET_TYPE='friend',
                            TARGET_VERIFIED=True,REAL_SEND_APPROVED=True):
            with patch.object(bridge,'send_message',return_value=result) as gui:
                for content in bodies:
                    self.assertEqual(self.post(self.form(content=content)),(200,'sent'))
                    self.assertEqual(gui.call_args.args,('托盘选择的测试好友',content))
                    self.assertFalse(gui.call_args.kwargs['dry_run'])
                self.assertEqual(gui.call_count,len(bodies))
                # Same full body remains a duplicate; never send a second time.
                self.assertEqual(self.post(self.form(content=bodies[0])),(200,'duplicate'))
                self.assertEqual(gui.call_count,len(bodies))
        self.assertNotIn('略略略',self.output.getvalue())
        self.assertNotIn('仅作模拟',self.output.getvalue())
        self.assertNotIn('略略略'.encode(),Path(self.db_path).read_bytes())

    def test_signed_full_sms_uses_same_queue_for_group_or_auto_chat(self):
        result=SendResult(True,'complete',attempted=True,verified=True)
        for kind in ('group','chat'):
            body=f'【无效群聊模拟{kind}】验证码：略略略'
            with patch.multiple(bridge,DRY_RUN=False,TARGET='示例群聊',TARGET_TYPE=kind,
                                TARGET_VERIFIED=True,REAL_SEND_APPROVED=True):
                with patch.object(bridge,'send_message',return_value=result) as gui:
                    self.assertEqual(self.post(self.form(content=body)),(200,'sent'))
                    self.assertEqual(gui.call_args.args,('示例群聊',body))
                    self.assertEqual(gui.call_args.kwargs['target_type'],kind)
                    self.assertEqual(self.post(self.form(content=body)),(200,'duplicate'))
                    self.assertEqual(gui.call_count,1)

    def test_double_encoded_official_signature(self):
        stamp = str(round(time.time() * 1000))
        self.assertEqual(self.post(self.form(timestamp=stamp, sign=quote(bridge.sign_for(stamp, bridge.SECRET), safe=''))),
                         (200, 'dry_run'))

    def test_duplicate_request_and_restart(self):
        body = self.form()
        self.assertEqual(self.post(body), (200, 'dry_run'))
        self.assertEqual(self.post(body), (200, 'duplicate'))
        self.stop_server()
        self.start_server()
        self.assertEqual(self.post(body), (200, 'duplicate'))

    def test_send_timeout_blocks_retry_after_restart(self):
        self.server.sender.send = Mock(side_effect=TimeoutError('synthetic content must never be logged'))
        body = self.form()
        self.assertEqual(self.post(body)[0], 409)
        self.assertEqual(self.server.sender.send.call_count, 1)
        self.assertNotIn('synthetic content must never be logged', self.output.getvalue())
        self.stop_server()
        self.start_server()
        self.server.sender.send = Mock()
        self.assertEqual(self.post(body)[0], 409)
        self.server.sender.send.assert_not_called()

    def test_client_disconnect_after_processing_does_not_resend(self):
        body = self.form().encode()
        with socket.create_connection(('127.0.0.1', self.port), timeout=2) as sock:
            header = f'POST /sms HTTP/1.0\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: {len(body)}\r\n\r\n'
            sock.sendall(header.encode() + body)
        # A concurrent retry can see the durable in-flight reservation. Neither
        # pending nor completed requests may trigger a second send.
        deadline=time.monotonic()+1
        while True:
            response=self.post(body.decode())
            if response==(200,'duplicate'):
                break
            self.assertEqual(response[0],409)
            self.assertLess(time.monotonic(),deadline)
            time.sleep(0.01)

    def test_slow_gui_does_not_block_http_validation_and_duplicate_never_resends(self):
        entered=threading.Event(); release=threading.Event()
        calls=[]
        def slow_send(message,**kwargs):
            calls.append(threading.current_thread().name)
            entered.set()
            self.assertTrue(release.wait(1))
            return SendResult(True,'complete',dry_run=True)
        self.server.sender.send=slow_send
        body=self.form(); results=[]
        request=threading.Thread(target=lambda:results.append(self.post(body)))
        request.start()
        self.assertTrue(entered.wait(1))
        try:
            self.assertEqual(self.post(self.form(sign='bad')),(403,'invalid signature'))
            self.assertEqual(self.post(body)[0],409)
        finally:
            release.set();request.join(2)
        self.assertEqual(results,[(200,'dry_run')])
        self.assertEqual(calls,['wechat-send-queue'])

    def test_false_or_unverified_result_is_not_success_or_retried(self):
        for index,result in enumerate((SendResult(False,'title','private input must not be logged'),None,
                                       SendResult(True,'complete',verified=False,attempted=True))):
            with self.subTest(result=result), patch.object(bridge,'DRY_RUN',False):
                self.server.send_halted.clear()  # Independent synthetic cases.
                self.server.sender.send=Mock(return_value=result)
                body=self.form(content=f'【无效模拟{index}】验证码 000000')
                self.assertEqual(self.post(body)[0],409)
                self.assertEqual(self.post(body)[0],409)
                self.assertEqual(self.server.sender.send.call_count,1)
        self.assertNotIn('private input must not be logged',self.output.getvalue())

    def test_unavailable_desktop_halts_following_gui_sends(self):
        with patch.object(bridge,'DRY_RUN',False):
            self.server.sender.send=Mock(return_value=SendResult(False,'desktop','input_desktop_unavailable'))
            self.assertEqual(self.post(self.form(content='【无效模拟甲】验证码 000000'))[0],409)
            self.assertTrue(self.server.send_halted.is_set())
            self.assertEqual(self.post(self.form(content='【无效模拟乙】验证码 000001'))[0],409)
            self.assertEqual(self.server.sender.send.call_count,1)

    def test_expired_queued_job_never_calls_sender(self):
        self.server.sender.send=Mock()
        job=bridge.SendJob('验证码：000000',time.time()-1)
        self.server.jobs.put(job)
        result=job.future.result(timeout=1)
        self.assertFalse(result.success)
        self.assertEqual(result.stage,'expired')
        self.server.sender.send.assert_not_called()

    def test_slow_or_incomplete_request_times_out_and_server_recovers(self):
        with socket.create_connection(('127.0.0.1', self.port), timeout=2) as sock:
            sock.sendall(b'POST /sms HTTP/1.0\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: 100\r\n\r\nx')
            received = b''
            while True:
                chunk = sock.recv(2048)
                if not chunk:
                    break
                received += chunk
            self.assertIn(b'408', received)
        self.assertEqual(self.post(), (200, 'dry_run'))

    def test_invalid_path_type_size_and_duplicate_fields(self):
        self.assertEqual(self.post(path='/other')[0], 404)
        self.assertEqual(self.post(content_type='application/json')[0], 415)
        self.assertEqual(self.post(body='x' * 8193)[0], 413)
        self.assertEqual(self.post(body=self.form() + '&from=other')[0], 400)

    def test_full_queue_rejects_without_gui_and_retains_reservation(self):
        self.server.sender.send=Mock()
        body=self.form()
        with patch.object(self.server.jobs,'put_nowait',side_effect=queue.Full):
            self.assertEqual(self.post(body)[0],409)
        self.assertEqual(self.post(body)[0],409)
        self.server.sender.send.assert_not_called()
        with self.server.db_lock:
            self.assertEqual(self.server.db.execute('SELECT count(*) FROM sent').fetchone()[0],0)
            self.assertEqual(self.server.db.execute('SELECT count(*) FROM uncertain').fetchone()[0],1)

    def test_handler_exception_has_fixed_error_no_input_disclosure(self):
        self.server.handle_verified_sms=Mock(side_effect=RuntimeError('private-message-must-not-appear'))
        self.assertEqual(self.post()[0],409)
        self.assertNotIn('private-message-must-not-appear',self.output.getvalue())

    def test_queue_exception_pauses_following_real_gui_jobs(self):
        with patch.object(bridge,'DRY_RUN',False):
            self.server.sender.send=Mock(side_effect=RuntimeError('private-window-text'))
            self.assertEqual(self.post(self.form(content='【异常甲】验证码 000000'))[0],409)
            self.assertTrue(self.server.send_halted.is_set())
            self.assertEqual(self.post(self.form(content='【异常乙】验证码 000001'))[0],409)
            self.assertEqual(self.server.sender.send.call_count,1)
            self.assertNotIn('private-window-text',self.output.getvalue())

    def test_repeated_nonsending_requests_keep_http_and_queue_healthy(self):
        self.server.sender.send=Mock()
        for index in range(30):
            self.assertEqual(self.post(self.form(content=f'普通连通测试{index}')),(200,'ignored'))
        self.server.sender.send.assert_not_called()
        self.assertTrue(self.thread.is_alive() and self.server.send_thread.is_alive())
        self.assertEqual(self.server.jobs.unfinished_tasks,0)

    def test_input_strings_reach_existing_sender_queue_without_ocr_or_zero_loss(self):
        result=SendResult(True,'complete',attempted=True,verified=True)
        values=('000008','123456','00123456')
        with patch.multiple(bridge,DRY_RUN=False,TARGET='示例好友',TARGET_TYPE='friend',
                            TARGET_VERIFIED=True,REAL_SEND_APPROVED=True):
            with patch.object(bridge,'send_message',return_value=result) as gui:
                for index,value in enumerate(values):
                    self.assertEqual(self.post(self.form(content=f'【隔离精度测试{index}】验证码：{value}')),(200,'sent'))
                    args,kwargs=gui.call_args
                    self.assertEqual(args,('示例好友',f'【隔离精度测试{index}】验证码：{value}'))
                    self.assertIsInstance(args[1],str)
                    self.assertFalse(kwargs['dry_run'])
                    self.assertTrue(kwargs['approved'])
                    self.assertGreater(kwargs['expires_at'],time.time())
                self.assertEqual(gui.call_count,len(values))
        self.assertEqual(self.server.jobs.unfinished_tasks,0)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch.multiple(bridge, SECRET='test-' + 'x' * 40, SENDERS={'+8600000000000'},
                                      TARGET='示例群聊', TARGET_TYPE='group', BIND='100.64.0.2',
                                      PORT=8765, CLIENT_IPS={'100.64.0.3'}, DRY_RUN=True,
                                      REAL_SEND_APPROVED=False, TARGET_VERIFIED=False)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_valid_config_and_refuse_public_or_wildcard_bind(self):
        bridge.validate_configuration()
        for value in ('0.0.0.0', '192.168.1.5', '8.8.8.8', '::'):
            with patch.object(bridge, 'BIND', value), self.assertRaises(ValueError):
                bridge.validate_configuration()

    def test_empty_or_regex_sender_and_client_allowlist_refused(self):
        for values in (set(), {'106.*'}, {'*'}):
            with patch.object(bridge, 'SENDERS', values), self.assertRaises(ValueError):
                bridge.validate_configuration()
        with patch.object(bridge, 'CLIENT_IPS', set()), self.assertRaises(ValueError):
            bridge.validate_configuration()

    def test_real_send_requires_approval_and_verified_target_title(self):
        with patch.object(bridge, 'DRY_RUN', False), self.assertRaises(ValueError):
            bridge.validate_configuration()

    def test_invalid_dry_run_flag_refused(self):
        with patch.dict(os.environ, {'SMS_DRY_RUN': 'false'}), self.assertRaises(ValueError):
            bridge.validate_configuration()

    def test_unapproved_sender_never_uses_wechat(self):
        with patch.object(bridge, 'DRY_RUN', False), patch.object(bridge, 'send_message') as send:
            sender = bridge.WeChatSender()
            with self.assertRaises(RuntimeError):
                sender.send('验证码：000000')
            send.assert_not_called()

    def test_friend_group_auto_chat_use_same_approved_sender(self):
        result=SendResult(True,'complete',verified=True,attempted=True)
        for kind in ('friend','group','chat'):
            with patch.multiple(bridge,DRY_RUN=False,REAL_SEND_APPROVED=True,TARGET_VERIFIED=True,TARGET_TYPE=kind):
                with patch.object(bridge,'send_message',return_value=result) as send:
                    self.assertEqual(bridge.WeChatSender().send('【全文模拟】验证码：略略略'),result)
                    send.assert_called_once_with(bridge.TARGET,'【全文模拟】验证码：略略略',
                        dry_run=False,approved=True,expires_at=None,target_type=kind)

    def test_unified_sender_result_not_exception_or_function_return(self):
        result=SendResult(False,'title','chat_title_not_exact')
        with patch.multiple(bridge,DRY_RUN=False,REAL_SEND_APPROVED=True,TARGET_VERIFIED=True,TARGET_TYPE='friend'):
            with patch.object(bridge,'send_message',return_value=result) as send:
                self.assertEqual(bridge.WeChatSender().send('验证码：000000'),result)
                send.assert_called_once_with(bridge.TARGET,'验证码：000000',dry_run=False,approved=True,expires_at=None,target_type='friend')


if __name__ == '__main__':
    unittest.main()
