"""One fixed invalid SMS -> authenticated HTTP -> queue -> visible WeChat test.

No production flags are changed. Other SMS bodies are ignored in this bounded
acceptance listener. Reuses the production Handler and database reservation.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import http.client
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlencode

import bridge
from wechat_sender import SendResult, SUPPORTED_VERSION

def send_message(*args, **kwargs):
    raise RuntimeError("Mock-only helper: real sending disabled")

SOURCE = '+8600000000000'
RECIPIENT = '示例好友'
CASES = {
    'synthetic': ('【本机整链路测试】验证码：000008；无效模拟码，请勿用于登录。', '验证码：000008'),
    'phone': ('【手机整链路测试】验证码：000009；无效模拟码，请勿用于登录。', '验证码：000009'),
    'synthetic_v2': ('【本机输入修复验收】验证码：000018；无效模拟码，请勿用于登录。', '验证码：000018'),
    'phone_v2': ('【手机输入修复验收】验证码：00123458；无效模拟码，请勿用于登录。', '验证码：00123458'),
    'synthetic_v3': ('【本机粘贴链路测试】验证码：000028；无效模拟码，请勿用于登录。', '验证码：000028'),
    'phone_v3': ('【手机粘贴链路测试】验证码：00123468；无效模拟码，请勿用于登录。', '验证码：00123468'),
    'phone_v4': ('【手机焦点修复测试】验证码：00123478；无效模拟码，请勿用于登录。', '验证码：00123478'),
    'phone_full_v1': ('【手机全文链路测试】验证码：00123478；无效模拟码，请勿用于登录。',
                      '【手机全文链路测试】验证码：00123478；无效模拟码，请勿用于登录。'),
}


def expected_queue_message(case):
    # Production now queues the full body. Historical test sends must still
    # map ONLY their authenticated fixed input to the originally approved text.
    return CASES[case][0]


class FixedTestSender:
    def __init__(self, case, root):
        self.case = case
        self.marker = root / f'ocr-acceptance-chain-{case}.json'
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.result = None
        self.gui_ms = None
        self.queue_thread_confirmed = False
        self.approved_full_text=None

    def send(self, message, *, expires_at=None):
        with self.lock:
            expected = self.approved_full_text if self.case == 'phone_full_v1' else expected_queue_message(self.case)
            if expected is None or message != expected or self.marker.exists():
                return SendResult(False, 'policy', 'fixed_test_only_or_previous_attempt')
            # A crash/timeout also leaves a durable no-repeat marker.
            try:
                with self.marker.open('x', encoding='utf-8') as record:
                    json.dump({'in_progress': True,'case':self.case,'recipient':RECIPIENT,
                        'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())},record)
                    record.flush()
                    os.fsync(record.fileno())
            except FileExistsError:
                return SendResult(False,'policy','prior_test_attempt_no_retry')
            self.queue_thread_confirmed=threading.current_thread().name=='wechat-send-queue'
            started=time.perf_counter()
            outgoing=self.approved_full_text if self.case=='phone_full_v1' else CASES[self.case][1]
            if outgoing is None:
                outcome=SendResult(False,'policy','no_authenticated_test_body')
            else:
                outcome = send_message(RECIPIENT, outgoing, dry_run=False,
                                   approved=True, expires_at=expires_at, target_type='friend')
            self.gui_ms=round((time.perf_counter()-started)*1000,1)
            proof = asdict(outcome)
            proof.update(recipient=RECIPIENT, client_version=SUPPORTED_VERSION,
                         sender_backend='paddle_ocr_local', case=self.case,
                         gui_ms=self.gui_ms,queue_thread_confirmed=self.queue_thread_confirmed,
                         utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
            self.marker.write_text(json.dumps(proof), encoding='utf-8')
            self.result = outcome
            self.finished.set()
            return outcome


class FixedTestServer(bridge.BridgeServer):
    def __init__(self,*args):
        super().__init__(*args)
        self.fixed_request_thread=None
        self.completed=threading.Event()
        self.request_context=threading.local()
        self.evidence={}

    def handle_verified_sms(self, sender, content, expires_at=None):
        # Gate the COMPLETE SMS before extraction; a real SMS that happens
        # to contain the same digits must never reach the GUI.
        full_test=self.test_case=='phone_full_v1'
        body_matches=('验证码：00123478' in content and bridge.extract_otp(content)=='00123478'
            if full_test else content==CASES[self.test_case][0])
        if (not bridge.ALLOW_ALL_SENDERS and sender != SOURCE) or not body_matches:
            return 'ignored'
        if full_test:
            # Human-authorized phrase match for this invalid mock code only.
            # Pin the first authenticated body in RAM, never log/persist it,
            # and prevent another request from replacing a queued full SMS.
            with self.sender.lock:
                if self.sender.approved_full_text is None:
                    self.sender.approved_full_text=content
                elif self.sender.approved_full_text!=content:
                    return 'ignored'
        if self.fixed_request_thread is None:
            self.fixed_request_thread=threading.get_ident()
            self.evidence.update(source_ip='pass',sender_allowlist='disabled_by_user' if bridge.ALLOW_ALL_SENDERS else 'pass',timestamp='pass',
                hmac='pass',fixed_body='pass',body_policy='contains_fixed_invalid_code' if full_test else 'exact_full_body',otp_extraction='pass' if
                f'验证码：{bridge.extract_otp(content)}'==('验证码：00123478' if full_test else CASES[self.test_case][1]) else 'fail')
        return super().handle_verified_sms(sender, content, expires_at)


class AcceptanceHandler(bridge.Handler):
    def do_POST(self):
        self.acceptance_started=time.perf_counter()
        super().do_POST()

    def reply(self,status,message):
        super().reply(status,message)
        if self.server.fixed_request_thread==threading.get_ident():
            self.server.evidence.update(http_status=status,http_result=message,
                peer_ip=self.client_address[0],webhook_ms=round((time.perf_counter()-self.acceptance_started)*1000,1))
            self.server.completed.set()
