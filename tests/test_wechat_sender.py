import sys
import io
import json
import subprocess
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock,patch
from wechat_sender import Line,HELPER,SenderError,choose_contact,send_message,VisualSender,NativeDesktop

def label(text,y,x=40,score=1):
    return Line(text,x,y,60,20,score)

def entry(text,y,score=1):
    return Line(text,100,y,100,20,score)

class IdentityTests(unittest.TestCase):
    def test_exact_friend_in_contacts_only(self):
        name='短信测试好友'
        chosen=entry(name,80)
        self.assertEqual(choose_contact([label('联系人',40),chosen,label('群聊',120),entry('其他群聊',160)],name),chosen)
        with self.assertRaisesRegex(SenderError, 'ambiguous_search_results'):
            choose_contact([label('联系人',40),chosen,label('群聊',120),entry(name,160)],name)

    def test_no_current_chat_fallback_or_official_history_choice(self):
        for section in ('公众号','聊天记录','服务号'):
            with self.subTest(section=section),self.assertRaises(SenderError):
                choose_contact([label(section,40),entry('短信测试好友',80)],'短信测试好友')

    def test_auto_first_group_or_friend_and_never_selects_later_matching_row(self):
        for section in ('群聊','联系人','最常使用'):
            selected=entry('示例群聊',80)
            self.assertEqual(choose_contact([label(section,40),selected],'示例群聊'),selected)
        for section in ('群聊','联系人','公众号','聊天记录'):
            with self.subTest(section=section),self.assertRaises(SenderError):
                choose_contact([label(section,40),entry('其他聊天',80),
                                label('群聊',120),entry('示例群聊',160)],'示例群聊')

    def test_explicit_type_and_group_member_counter_are_not_fuzzy_matches(self):
        group=[label('群聊',40),entry('示例群聊 (3)',80)]
        self.assertEqual(choose_contact(group,'示例群聊',target_type='group').text,'示例群聊 (3)')
        for rows,kind in ((group,'friend'),([label('联系人',40),entry('示例群聊',80)],'group'),
                          ([label('群聊',40),entry('示例群聊其他',80)],'chat'),
                          ([label('群聊',40,score=.8),entry('示例群聊',80)],'chat')):
            with self.assertRaises(SenderError):choose_contact(rows,'示例群聊',target_type=kind)

    def test_group_title_accepts_only_exact_name_and_standard_member_count(self):
        for kind in ('group','chat'):
            visual=VisualSender(Mock(scale=1,width=1200),Mock());visual.selected_chat_type=kind
            for lines in ([entry('示例群聊',50)],[entry('示例群聊 (3)',50)],[entry('示例群聊（3）',50)],
                          [entry('示例群聊',50),Line('(3)',240,50,30,20)]):
                visual.read=Mock(return_value=lines)
                visual.title_verified('示例群聊')
            for text in ('示例群聊其他','示例群聊3','其他群(3)','示例群聊(3)多余'):
                visual.read=Mock(return_value=[entry(text,50)])
                with self.assertRaises(SenderError):visual.title_verified('示例群聊')
        visual.selected_chat_type='friend'
        visual.read=Mock(return_value=[entry('示例群聊(3)',50)])
        with self.assertRaises(SenderError):visual.title_verified('示例群聊')

    def test_first_fuzzy_or_low_confidence_result_refused(self):
        for rows in ([label('联系人',40),entry('短信测试好友甲',80)],
                     [label('联系人',40),entry('短信测试好友',80,0.9)],
                     [label('联系人',40),entry('其他好友',80),entry('短信测试好友',120)]):
            with self.assertRaises(SenderError):
                choose_contact(rows,'短信测试好友')

    def test_duplicate_names_stop_before_enter_even_in_most_used(self):
        chosen=entry('短信测试好友',80)
        for category in ('联系人','最常使用'):
            with self.assertRaisesRegex(SenderError, 'ambiguous_search_results'):
                choose_contact([label(category,40),Line('②',390,79,20,20,0.36),chosen,entry('短信测试好友',120)],'短信测试好友')

    def test_truncated_results_cannot_establish_unique_identity(self):
        with self.assertRaisesRegex(SenderError, 'search_results_incomplete'):
            choose_contact([label('联系人',40),entry('示例好友',80),label('更多联系人',160)],'示例好友')

    def test_pipe_is_never_dropped_or_aliased(self):
        with self.assertRaises(SenderError):
            choose_contact([label('联系人',40),entry('示例好友',80)],'示例|好友')

    def test_unknown_or_low_confidence_group_header_ends_contact_section(self):
        for group in (label('群聊',120,score=0.8),label('[unconfirmed]',120)):
            with self.assertRaises(SenderError):
                choose_contact([label('联系人',40),entry('其他好友',80),group,entry('短信测试好友',160)],'短信测试好友')

    def test_helper_has_explicit_function_category_only(self):
        self.assertEqual(choose_contact([label('功能',40),entry(HELPER,80)],HELPER).text,HELPER)
        with self.assertRaises(SenderError):
            choose_contact([label('聊天记录',40),entry(HELPER,80)],HELPER)

    def test_header_mismatch_stops(self):
        desktop=Mock(scale=1,width=1200)
        visual=VisualSender(desktop,Mock())
        visual.read=Mock(return_value=[entry('其他会话',50)])
        with self.assertRaises(SenderError) as caught:
            visual.title_verified('短信测试好友')
        self.assertEqual(caught.exception.stage,'title')
        desktop.key.assert_not_called()

    def test_dry_run_has_no_typing_or_send_key(self):
        desktop=Mock()
        visual=VisualSender(desktop,Mock())
        visual.open_contact=Mock()
        result=visual.send('短信测试好友','本机普通消息测试',dry_run=True)
        self.assertTrue(result.success and result.dry_run and result.verified)
        self.assertFalse(result.attempted)
        desktop.key.assert_not_called(); desktop.type_text.assert_not_called()

class PolicyTests(unittest.TestCase):
    def clipboard_desktop(self):
        sequence=[1];data={};writes=[];reads=[]
        def empty():
            data.clear();sequence[0]+=1
        def put(fmt,value):
            data[fmt]=value;writes.append((fmt,value));sequence[0]+=1
        def get(fmt):
            reads.append(fmt);return data[fmt]
        clipboard=SimpleNamespace(CF_UNICODETEXT=13,OpenClipboard=Mock(),CloseClipboard=Mock(),
            EmptyClipboard=Mock(side_effect=empty),RegisterClipboardFormat=lambda name:name,
            SetClipboardData=Mock(side_effect=put),GetClipboardData=Mock(side_effect=get))
        desktop=object.__new__(NativeDesktop);desktop.hwnd=123;desktop.guard=Mock();desktop.api=Mock();desktop.key=Mock()
        desktop.u32=SimpleNamespace(GetAsyncKeyState=lambda key:0,GetClipboardSequenceNumber=lambda:sequence[0])
        return desktop,clipboard,sequence,data,writes,reads

    def test_private_clipboard_sets_exclusions_before_plaintext_and_cleans_once(self):
        d,clipboard,sequence,data,writes,reads=self.clipboard_desktop()
        text='模拟短信全文\n验证码：00123458'
        with patch.dict(sys.modules,win32clipboard=clipboard),patch('wechat_sender.time.sleep'):
            d.type_text(text)
        self.assertEqual([name for name,_ in writes[:3]],['ExcludeClipboardContentFromMonitorProcessing',
            'CanIncludeInClipboardHistory','CanUploadToCloudClipboard'])
        self.assertTrue(all(value==b'\0'*4 for _,value in writes[:3]))
        self.assertEqual(writes[-1],(13,text))
        self.assertNotIn(13,reads)  # Prior text/history is never read.
        self.assertEqual(data,{})
        d.key.assert_called_once_with(86)
        self.assertEqual([call.args for call in d.api.keybd_event.call_args_list],[(17,0,0),(17,0,2)])

    def test_privacy_failure_never_places_plaintext_or_pastes(self):
        d,clipboard,_,_,writes,_=self.clipboard_desktop()
        clipboard.GetClipboardData=Mock(return_value=b'\1\0\0\0')
        with patch.dict(sys.modules,win32clipboard=clipboard),self.assertRaises(SenderError):
            d.type_text('验证码：000008')
        self.assertNotIn(13,[fmt for fmt,_ in writes]);d.key.assert_not_called()

    def test_paste_uses_sequence_committed_on_clipboard_close(self):
        d,clipboard,sequence,data,_,_=self.clipboard_desktop()
        def commit():
            sequence[0]+=1
        clipboard.CloseClipboard.side_effect=commit
        with patch.dict(sys.modules,win32clipboard=clipboard),patch('wechat_sender.time.sleep'):
            d.type_text('验证码：000008')
        d.key.assert_called_once_with(86)
        self.assertEqual(data,{})

    def test_paste_failure_releases_ctrl_and_clears_own_clipboard_without_retry(self):
        d,clipboard,_,data,_,_=self.clipboard_desktop()
        d.key.side_effect=SenderError('desktop','wechat_not_foreground')
        with patch.dict(sys.modules,win32clipboard=clipboard),self.assertRaises(SenderError):
            d.type_text('000008')
        self.assertEqual(data,{})
        d.key.assert_called_once_with(86)
        self.assertEqual([call.args for call in d.api.keybd_event.call_args_list],[(17,0,0),(17,0,2)])

    def test_new_human_clipboard_after_paste_is_not_deleted(self):
        d,clipboard,sequence,data,_,_=self.clipboard_desktop()
        def human_copy(vk):
            data.clear();data[13]='new human clipboard';sequence[0]+=1
        d.key.side_effect=human_copy
        with patch.dict(sys.modules,win32clipboard=clipboard),patch('wechat_sender.time.sleep'):
            d.type_text('000008')
        self.assertEqual(data[13],'new human clipboard')
        self.assertEqual(clipboard.EmptyClipboard.call_count,1)

    def test_held_modifier_blocks_text_without_synthetic_release(self):
        desktop,clipboard,_,_,_,_=self.clipboard_desktop()
        desktop.u32.GetAsyncKeyState=lambda key:0x8000 if key==17 else 0
        with patch.dict(sys.modules,win32clipboard=clipboard),self.assertRaises(SenderError) as error:
            desktop.type_text('000008')
        self.assertEqual(error.exception.error,'held_key_before_text_input')
        clipboard.OpenClipboard.assert_not_called();desktop.api.keybd_event.assert_not_called()

    def test_requested_send_flow_checks_target_but_not_draft_text_or_bubble(self):
        d=Mock(scale=1,width=1200,height=900)
        sender=VisualSender(d,Mock());sender.open_contact=Mock();sender.title_verified=Mock()
        sender.editor_box=Mock(return_value=(300,750,1000,840));sender.input_empty=Mock(return_value=True)
        sender.read=Mock(return_value=[]);sender.latest_outgoing=Mock(side_effect=AssertionError('Bubble inspection cancelled'))
        with patch('wechat_sender.time.sleep'):
            result=sender.send('短信测试好友','验证码：000008',dry_run=False)
        self.assertTrue(result.success and result.attempted and result.verified)
        self.assertFalse(result.delivery_checked)
        d.type_text.assert_called_once_with('验证码：000008');d.key.assert_called_once_with(13)
        self.assertEqual(sender.read.call_count,1)  # Initial draft protection only.
        sender.latest_outgoing.assert_not_called()

    def test_foreground_denied_has_fixed_desktop_failure_without_input(self):
        desktop=object.__new__(NativeDesktop)
        desktop.hwnd=123;desktop.gui=Mock();desktop.gui.IsIconic.return_value=False
        desktop.gui.IsWindowVisible.return_value=True;desktop.gui.GetForegroundWindow.return_value=456
        desktop.gui.SetForegroundWindow.side_effect=OSError('private-window-details')
        desktop.rect=(0,0,1200,900);desktop.width=1200;desktop.scale=1
        desktop.gui.GetWindowRect.return_value=desktop.rect
        desktop.gui.GetAncestor.return_value=456  # Obstructed: never click another app.
        desktop.api=Mock()
        desktop.guard_desktop=Mock()
        with self.assertRaises(SenderError) as raised:
            desktop.activate()
        self.assertEqual(raised.exception.stage,'desktop')
        self.assertEqual(raised.exception.error,'foreground_activation_failed')
        desktop.api.mouse_event.assert_not_called()

    def test_visible_title_click_can_activate_without_foreground_policy_changes(self):
        desktop=object.__new__(NativeDesktop);desktop.hwnd=123;desktop.width=1200;desktop.scale=1
        desktop.rect=(10,20,1210,920);desktop.gui=Mock();desktop.api=Mock();desktop.u32=Mock()
        desktop.guard_desktop=Mock();desktop.guard=Mock()
        desktop.gui.IsIconic.return_value=False;desktop.gui.IsWindowVisible.return_value=True
        desktop.gui.IsWindowEnabled.return_value=True;desktop.gui.GetWindowRect.return_value=desktop.rect
        desktop.gui.GetAncestor.return_value=123
        desktop.gui.GetForegroundWindow.side_effect=[456,456,123]
        desktop.gui.SetForegroundWindow.side_effect=OSError('denied')
        with patch('wechat_sender.time.sleep'),patch('wechat_sender.ctypes.windll'):
            desktop.activate()
        desktop.gui.WindowFromPoint.assert_called_once_with((610,35))
        self.assertEqual([call.args for call in desktop.api.mouse_event.call_args_list],[(2,0,0),(4,0,0)])

    def test_worker_timeout_or_invalid_reply_is_unknown_and_never_retried(self):
        for failure in (subprocess.TimeoutExpired('local-worker',45),None):
            with patch('wechat_sender.subprocess.run') as child:
                if failure is not None:
                    child.side_effect=failure
                else:
                    child.return_value=Mock(stdout='not-a-valid-result')
                result=send_message('短信测试好友','模拟测试',dry_run=False,approved=True)
                self.assertFalse(result.success)
                self.assertTrue(result.attempted)
                self.assertEqual(result.stage,'worker')
                self.assertEqual(child.call_count,1)

    def test_missing_main_window_stops_without_capture_or_keyboard(self):
        desktop=object.__new__(NativeDesktop)
        desktop.hwnd=123;desktop.gui=Mock();desktop.gui.IsWindow.return_value=False
        desktop.guard_desktop=Mock()
        with self.assertRaises(SenderError) as raised:
            desktop.guard()
        self.assertEqual(raised.exception.error,'window_unavailable')
        desktop.gui.GetForegroundWindow.assert_not_called()

    def test_noninteractive_desktop_stops_before_window_actions(self):
        desktop=object.__new__(NativeDesktop)
        desktop.hwnd=123;desktop.gui=Mock()
        desktop.guard_desktop=Mock(side_effect=SenderError('desktop','desktop_locked_or_secure_prompt'))
        with self.assertRaises(SenderError):
            desktop.guard()
        desktop.gui.IsWindow.assert_not_called()

    def test_failed_search_and_existing_draft_never_receive_text_or_send(self):
        for search_failed in (True,False):
            desktop=Mock(scale=1,width=1200,height=900)
            sender=VisualSender(desktop,Mock())
            sender.open_contact=Mock(side_effect=SenderError('search','search_placeholder_unconfirmed') if search_failed else None)
            sender.editor_box=Mock(return_value=(300,750,1000,840))
            sender.read=Mock(return_value=[entry('已有草稿',780)])
            with self.assertRaises(SenderError) as raised:
                sender.send('短信测试好友','模拟测试',dry_run=False)
            self.assertEqual(raised.exception.stage,'search' if search_failed else 'input')
            desktop.type_text.assert_not_called();desktop.key.assert_not_called()

    def test_approval_expiry_and_unsafe_payload_stop_before_process(self):
        with patch('wechat_sender.subprocess.run') as worker:
            for recipient,text,kwargs in [('短信测试好友','普通测试',{'dry_run':False}),
                ('','普通测试',{}),('带 空格备注','普通测试',{}),('短信测试好友','消息\0截断',{}),
                ('短信测试好友','普通测试',{'expires_at':time.time()-1})]:
                self.assertFalse(send_message(recipient,text,**kwargs).success)
            worker.assert_not_called()

    def test_full_sms_over_300_characters_passes_text_validation_but_keeps_expiry_and_approval(self):
        from wechat_sender import MAX_MESSAGE_CHARS
        with patch('wechat_sender.subprocess.run') as worker:
            for text in ('验证码：'+ '无效模拟'*100, '验证码'+ 'x'*(MAX_MESSAGE_CHARS-3)):
                result=send_message('短信测试好友',text,dry_run=False,approved=True,expires_at=0)
                self.assertEqual(result.error,'message_expired')
                result=send_message('短信测试好友',text,dry_run=False,approved=False)
                self.assertEqual(result.error,'explicit_send_approval_required')
            result=send_message('短信测试好友','验证码'+ 'x'*MAX_MESSAGE_CHARS,dry_run=False,approved=True)
            self.assertEqual(result.error,'invalid_message_text')
            worker.assert_not_called()

    def test_unicode_full_sms_worker_pipe_does_not_truncate_escaped_json(self):
        import wechat_sender
        text='验证码：'+ '无效模拟'*400
        data={'recipient':'示例群聊','text':text,'dry_run':True,'approved':False,'target_type':'group'}
        wire=json.dumps(data)
        self.assertGreater(len(wire),8192)
        output=io.StringIO()
        with patch.object(sys,'argv',['wechat_sender.py','--worker']), \
             patch.object(sys,'stdin',io.StringIO(wire)),patch.object(sys,'stdout',output), \
             patch('wechat_sender.send_message',return_value=wechat_sender.SendResult(True,'complete',verified=True,dry_run=True)) as send:
            wechat_sender.main()
        send.assert_called_once_with(**data)
        self.assertTrue(json.loads(output.getvalue())['success'])

    def test_keyboard_select_all_releases_modifier(self):
        desktop=object.__new__(NativeDesktop)
        desktop.guard=Mock();desktop.api=Mock()
        with patch('wechat_sender.time.sleep'):
            desktop.select_all()
        events=[call.args for call in desktop.api.keybd_event.call_args_list]
        self.assertEqual(events,[(17,0,0),(65,0,0),(65,0,2),(17,0,2)])

if __name__=='__main__':
    unittest.main()
