import tempfile
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

import bridge
from chain_acceptance import CASES, SOURCE, FixedTestSender, FixedTestServer,expected_queue_message
from wechat_sender import SendResult


class ControlledAcceptanceTests(unittest.TestCase):
    def test_new_fixed_round_never_accepts_prior_failed_body_or_same_digits_alone(self):
        for case,legacy in (('synthetic_v2','synthetic'),('phone_v2','phone'),
                            ('synthetic_v3','synthetic_v2'),('phone_v3','phone_v2'),('phone_v4','phone_v3')):
            server=object.__new__(FixedTestServer)
            server.test_case=case;server.fixed_request_thread=None;server.evidence={}
            with patch.object(bridge.BridgeServer,'handle_verified_sms',return_value='sent') as base:
                for body in (CASES[legacy][0],CASES[case][1],CASES[case][0]+'extra'):
                    self.assertEqual(server.handle_verified_sms(SOURCE,body),'ignored')
                base.assert_not_called()
                self.assertEqual(server.handle_verified_sms(SOURCE,CASES[case][0]),'sent')
                base.assert_called_once()

    def test_whole_body_and_sender_gate_before_production_extraction(self):
        server = object.__new__(FixedTestServer)
        server.test_case = 'phone'
        server.fixed_request_thread=None
        server.evidence={}
        with patch.object(bridge.BridgeServer, 'handle_verified_sms', return_value='sent') as base:
            for source, body in [(SOURCE, '验证码：000009'),
                                 ('00000000000', CASES['phone'][0]),
                                 (SOURCE, CASES['phone'][0] + 'extra')]:
                self.assertEqual(server.handle_verified_sms(source, body), 'ignored')
            base.assert_not_called()
            self.assertEqual(server.handle_verified_sms(SOURCE, CASES['phone'][0], 123), 'sent')
            base.assert_called_once_with(SOURCE, CASES['phone'][0], 123)

    def test_durable_marker_prevents_repeat_and_captures_failure_without_text(self):
        with tempfile.TemporaryDirectory() as folder:
            sender = FixedTestSender('synthetic', Path(folder))
            failure = SendResult(False, 'title', 'chat_title_not_exact')
            with patch('chain_acceptance.send_message', return_value=failure) as gui:
                self.assertFalse(sender.send('验证码：123456').success)
                gui.assert_not_called()
                self.assertEqual(sender.send(expected_queue_message('synthetic'), expires_at=time.time()+20), failure)
                self.assertFalse(sender.send(expected_queue_message('synthetic')).success)
                self.assertEqual(gui.call_count, 1)
            proof = sender.marker.read_text(encoding='utf-8')
            self.assertNotIn('000008', proof)
            self.assertTrue(sender.finished.is_set())

    def test_atomic_creation_refuses_concurrent_existing_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            sender=FixedTestSender('synthetic',Path(folder))
            sender.marker.write_text('existing-attempt',encoding='utf-8')
            with patch.object(Path,'exists',return_value=False),patch('chain_acceptance.send_message') as gui:
                self.assertFalse(sender.send(expected_queue_message('synthetic')).success)
                gui.assert_not_called()
            self.assertEqual(sender.marker.read_text(encoding='utf-8'),'existing-attempt')

    def test_full_sms_test_accepts_only_authenticated_queued_body_without_repeating(self):
        case='phone_full_v1'
        with tempfile.TemporaryDirectory() as folder:
            sender=FixedTestSender(case,Path(folder))
            sender.approved_full_text=CASES[case][0]
            success=SendResult(True,'complete',attempted=True,verified=True)
            with patch('chain_acceptance.send_message',return_value=success) as gui:
                self.assertFalse(sender.send('验证码：000018').success)
                gui.assert_not_called()
                self.assertEqual(sender.send(expected_queue_message(case)),success)
                self.assertEqual(gui.call_args.args,('示例好友',CASES[case][0]))
                self.assertFalse(sender.send(expected_queue_message(case)).success)
                self.assertEqual(gui.call_count,1)

    def test_historical_digit_only_send_does_not_expand_to_new_full_sms_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            sender=FixedTestSender('synthetic',Path(folder))
            success=SendResult(True,'complete',attempted=True,verified=True)
            with patch('chain_acceptance.send_message',return_value=success) as gui:
                self.assertFalse(sender.send(CASES['synthetic'][1]).success)
                self.assertEqual(sender.send(CASES['synthetic'][0]),success)
                self.assertEqual(gui.call_args.args,('示例好友',CASES['synthetic'][1]))
                self.assertEqual(gui.call_count,1)

    def test_phrase_body_gate_pins_first_authenticated_full_mock_and_refuses_other_codes(self):
        with tempfile.TemporaryDirectory() as folder:
            server=object.__new__(FixedTestServer);server.test_case='phone_full_v1'
            server.fixed_request_thread=None;server.evidence={};server.sender=FixedTestSender('phone_full_v1',Path(folder))
            first='仅此模拟\n验证码：00123478'
            with patch.object(bridge.BridgeServer,'handle_verified_sms',return_value='sent') as base:
                for source,body in [(SOURCE,'验证码：00123468'),('other',first),
                                    (SOURCE,'验证码：00123478；验证码：123456')]:
                    self.assertEqual(server.handle_verified_sms(source,body),'ignored')
                base.assert_not_called()
                self.assertEqual(server.handle_verified_sms(SOURCE,first),'sent')
                self.assertEqual(server.sender.approved_full_text,first)
                self.assertEqual(server.handle_verified_sms(SOURCE,first+'额外内容'),'ignored')
                self.assertEqual(base.call_count,1)


if __name__ == '__main__':
    unittest.main()
