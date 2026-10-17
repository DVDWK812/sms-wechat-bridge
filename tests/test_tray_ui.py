import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
import unittest
try:
    import tray_app
except ImportError:
    tray_app = None

@unittest.skipUnless(tray_app, 'Native UI tests run in the existing sender venv')
class TrayUITests(unittest.TestCase):
    def app(self):
        app=object.__new__(tray_app.Tray)
        app.hwnd=123;app.log=Mock();app.message=Mock();app.notify=Mock()
        app.quit_event=Mock();app.controller=Mock()
        return app

    def test_menu_contains_all_requested_choices_and_releases_native_menu(self):
        app=self.app()
        with patch.object(tray_app,'win32gui') as gui:
            gui.CreatePopupMenu.return_value=44;gui.GetCursorPos.return_value=(10,20)
            app.menu()
            self.assertEqual([call.args[3] for call in gui.AppendMenu.call_args_list],
                ['设计预览','开启转发','关闭转发','查看运行状态','更改联系人','短信筛选设置','开机启动设置','退出程序'])
            gui.DestroyMenu.assert_called_once_with(44)

    def test_declining_confirmation_never_calls_enable(self):
        app=self.app()
        with patch.object(tray_app,'read_json',return_value={'WECHAT_TARGET':'测试好友'}), \
             patch.object(tray_app.win32gui,'MessageBox',return_value=tray_app.win32con.IDNO):
            app.command(101)
        app.controller.enable.assert_not_called()

    def test_exit_closes_window_even_when_state_save_fails(self):
        app=self.app();app.controller.close.side_effect=OSError('private-unlogged-error')
        with patch.object(tray_app.win32gui,'DestroyWindow') as destroy:
            app.command(105)
        app.quit_event.set.assert_called_once()
        app.controller.close.assert_called_once()
        destroy.assert_called_once_with(123)

    def test_contact_menu_only_opens_editor_never_enables_or_sends(self):
        app=self.app()
        with patch('recipient_dialog.show_recipient_dialog') as show:
            app.command(106)
        self.assertEqual(show.call_args.args,(app.controller,))
        self.assertIn('on_ready',show.call_args.kwargs)
        self.assertFalse(app.recipient_editor_open)
        app.controller.enable.assert_not_called()

    def test_contact_editor_cannot_open_twice_and_exit_closes_it(self):
        app=self.app();app.recipient_editor_open=True
        with patch('recipient_dialog.show_recipient_dialog') as show:
            app.command(106)
        show.assert_not_called()
        app.recipient_editor_close=Mock()
        with patch.object(tray_app.win32gui,'DestroyWindow'):
            app.command(105)
        app.recipient_editor_close.assert_called_once()

    def test_ignored_result_shows_current_full_text_rule_without_mislabeling_history(self):
        app=self.app()
        app.controller.last={'last_result':'ignored'}
        app.controller.status='off';app.controller.error=''
        with patch.object(tray_app,'read_json',return_value={'WECHAT_TARGET':'测试好友'}):
            text=app.status_text()
        self.assertIn('正文包含关键字“验证码”：转发全文',text)
        self.assertNotIn('4–8',text)
        self.assertIn('未进入微信发送队列',text)

    def test_filter_menu_only_opens_settings_without_enabling_or_sending(self):
        app=self.app()
        with patch('filter_dialog.show_filter_dialog') as show:
            app.command(107)
        self.assertEqual(show.call_args.args,(app.controller,))
        self.assertIn('on_ready',show.call_args.kwargs)
        self.assertFalse(app.filter_editor_open)
        app.controller.enable.assert_not_called()

    def test_filter_editor_is_single_instance_and_exit_closes_it(self):
        app=self.app();app.filter_editor_open=True
        with patch('filter_dialog.show_filter_dialog') as show:
            app.command(107)
        show.assert_not_called()
        app.filter_editor_close=Mock()
        with patch.object(tray_app.win32gui,'DestroyWindow'):
            app.command(105)
        app.filter_editor_close.assert_called_once()

    def test_unrestricted_mode_is_shown_in_explicit_enable_confirmation(self):
        app=self.app()
        with patch.object(tray_app,'read_json',return_value={'SMS_FILTER_MODE':'all','SMS_FILTER_KEYWORD':''}), \
             patch.object(tray_app.win32gui,'MessageBox',return_value=tray_app.win32con.IDNO) as confirm:
            app.command(101)
        self.assertIn('转发全部非空短信全文',confirm.call_args.args[1])
        app.controller.enable.assert_not_called()

if __name__=='__main__':unittest.main()
