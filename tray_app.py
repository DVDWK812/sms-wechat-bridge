"""Current-user Windows notification-area switch. First launch is OFF."""
from __future__ import annotations
import hashlib
import argparse
import os
from pathlib import Path
import subprocess
import threading
import time
import win32api
import win32con
import win32event
import win32gui
import win32ts
from tray_control import Controller, PROJECT, ROOT, read_json, setup_logging, atomic_json
from sms_filter import describe_filter

LABEL = '短信 → 微信自动转发'
WM_TRAY, WM_REFRESH = win32con.WM_USER + 20, win32con.WM_USER + 21
STATE_NAMES = {'off': '已关闭', 'running': '运行中', 'starting': '正在启动', 'fault': '故障'}
ERROR_NAMES = {
    'tailscale_not_running': 'Tailscale 未连接', 'paired_phone_offline': '配对手机不在线',
    'tailscale_address_changed': 'Tailscale 地址与原配置不一致',
    'tailscale_health_unavailable': '无法检查 Tailscale',
    'logged_in_main_window_not_unique': '微信未登录或主窗口不可用',
    'client_version_requires_new_acceptance': '微信版本改变，需要重新验收',
    'desktop_locked_or_secure_prompt': 'Windows 已锁屏或处于安全提示桌面',
    'input_desktop_unavailable': '桌面不可交互', 'active_console_session_required': '当前不是活动控制台会话',
    'interactive_user_session_required': '需要已登录用户的交互式桌面',
    'sender_paused_manual_review': '发送端已暂停，请检查微信与未发送草稿后关闭再开启；不会重试旧任务',
    'receiver_crashed_no_message_retry': '接收器异常，正在恢复；不会重试旧任务',
    'authorization_or_target_changed': '授权或目标配置改变，需重新确认',
    'controller_failure_manual_review': '控制器故障，请关闭再开启',
    'receiver_start_timeout': '后台监听启动超时', 'wechat_dialog_blocking': '微信被弹窗阻挡',
    'friend_acceptance_missing_or_changed': '目标好友或群聊尚未核验或微信版本改变，请在“更改联系人”中核验',
    'explicit_tray_confirmation_required': '需要本人确认开启', 'receiver_start_failed': '后台进程启动失败',
    'desktop_health_unavailable': '无法检查 Windows 桌面',
    'windows_suspend': 'Windows 休眠中，发送已禁止', 'session_locked': 'Windows 锁屏中，发送已禁止',
    'disable_persistence_failed_sending_revoked': '实时发送已禁止，但关闭状态写入失败；请检查配置目录权限后再次关闭',
    'incomplete_request_manual_review': '存在未完成或结果不明确的请求，已暂停自动恢复；检查微信与草稿后关闭再开启，不重发旧请求',
    'recipient_verification_in_progress': '正在核验联系人，核验结束后才能手动开启转发',
    'recipient_identity_not_verified': '联系人或会话标题未通过核验，请在“更改联系人”中检查备注并重新核验',
    'recipient_verification_cancelled': '联系人核验已取消，转发保持关闭',
    'invalid_friend_remark': '请填写完整的好友备注或群聊名称，不含空格或控制字符',
    'invalid_sms_filter_mode': '短信筛选模式无效，请重新选择',
    'invalid_sms_filter_keyword': '关键字须为不含换行或控制字符的 1–128 字文本',
    'empty_sms_filter_keyword': '包含或不包含模式必须填写非空关键字',
}

def acquire_instance():
    identity = win32api.GetUserName()
    digest = hashlib.sha256((identity + str(PROJECT).casefold()).encode()).hexdigest()[:24]
    handle = win32event.CreateMutex(None, False, 'Local\\SmsWechatBridgeTray-' + digest)
    if win32event.WaitForSingleObject(handle, 0) not in (0, 128):
        handle.Close()
        return None
    return handle

def make_icons():
    from PIL import Image, ImageDraw
    folder = ROOT / 'tray-icons'
    folder.mkdir(parents=True, exist_ok=True)
    icons = {}
    for state, colour in [('off', '#73808c'), ('running', '#159957'), ('fault', '#dd3b34')]:
        image = Image.new('RGBA', (64, 64))
        draw = ImageDraw.Draw(image)
        draw.ellipse((3, 3, 61, 61), fill=colour)
        if state == 'running':
            draw.polygon([(24, 17), (24, 47), (47, 32)], fill='white')
        elif state == 'off':
            draw.rectangle((21, 20, 29, 44), fill='white'); draw.rectangle((36, 20, 44, 44), fill='white')
        else:
            draw.rectangle((29, 15, 35, 36), fill='white'); draw.ellipse((29, 43, 35, 49), fill='white')
        path = folder / (state + '.ico')
        image.save(path, sizes=[(16,16),(24,24),(32,32),(48,48),(64,64)])
        icons[state] = win32gui.LoadImage(0, str(path), win32con.IMAGE_ICON, 0, 0, win32con.LR_LOADFROMFILE)
    icons['starting'] = icons['off']
    return icons

class Tray:
    def __init__(self):
        atomic_json(ROOT/'tray-bootstrap.json', {'pid':os.getpid(),'phase':'logger'})
        self.log = setup_logging()
        atomic_json(ROOT/'tray-bootstrap.json', {'pid':os.getpid(),'phase':'first_log'})
        self.log.info('startup_phase=controller')
        atomic_json(ROOT/'tray-bootstrap.json', {'pid':os.getpid(),'phase':'controller'})
        self.controller = Controller()
        self.log.info('startup_phase=icons')
        self.icons = make_icons()
        self.quit_event = threading.Event()
        self.last_ui = None
        self.recipient_editor_open = False
        self.recipient_editor_close = None
        self.filter_editor_open = False
        self.filter_editor_close = None
        self.taskbar_restart = win32gui.RegisterWindowMessage('TaskbarCreated')
        instance = win32api.GetModuleHandle(None)
        cls = win32gui.WNDCLASS()
        cls.hInstance, cls.lpszClassName = instance, 'SmsWechatBridgeTrayWindow'
        cls.lpfnWndProc = self.window_proc
        atom = win32gui.RegisterClass(cls)
        self.hwnd = win32gui.CreateWindow(atom, LABEL, 0, 0, 0, 0, 0, 0, 0, instance, None)
        self.log.info('startup_phase=session_notifications')
        win32ts.WTSRegisterSessionNotification(self.hwnd, win32ts.NOTIFY_FOR_THIS_SESSION)
        self.log.info('startup_phase=tray_registration')
        self.notify(win32gui.NIM_ADD)
        self.log.info('startup_phase=initial_status')
        self.controller.tick()
        self.thread = threading.Thread(target=self.monitor, name='tray-supervisor', daemon=True)
        self.thread.start()
        self.log.info('tray_started; production_enabled=%s', self.controller.state['desired_enabled'])

    def notify(self, command=win32gui.NIM_MODIFY):
        state = self.controller.status
        tip = LABEL + '：' + STATE_NAMES.get(state, '故障')
        data = (self.hwnd, 1, win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
                WM_TRAY, self.icons.get(state, self.icons['fault']), tip)
        win32gui.Shell_NotifyIcon(command, data)

    def message(self, text, error=False):
        win32gui.MessageBox(self.hwnd, text, LABEL, win32con.MB_OK |
                            (win32con.MB_ICONERROR if error else win32con.MB_ICONINFORMATION))

    def status_text(self):
        c = self.controller
        last = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(c.last['last_time'])) if c.last.get('last_time') else '暂无'
        config = read_json(c.config_path)
        result = c.last.get('last_result', '暂无')
        if result == 'sent':
            result = '已对核验的目标执行发送键（不代表对端收件确认）'
        elif result == 'ignored':
            result = '已按处理时规则忽略，未进入微信发送队列'
        return (f'状态：{STATE_NAMES.get(c.status, "故障")}\n目标好友或群聊：{config.get("WECHAT_TARGET", "未设置")}\n'
                f'当前规则：{describe_filter(config)}\n'
                f'监听：{config.get("SMS_BIND", "")}:{config.get("SMS_PORT", "")}\n'
                f'最近处理：{last}\n结果：{result}\n发送阶段：{c.last.get("last_stage", "暂无")}\n'
                f'故障：{ERROR_NAMES.get(c.error, c.error) or "无"}\n'
                '关闭会禁止新发送并丢弃等待任务。已按出的发送键无法撤回。\n'
                '失败或结果不明确的任务不自动重试。状态中不显示验证码。')

    def menu(self):
        menu = win32gui.CreatePopupMenu()
        for number, label in [(100, '设计预览'), (101, '开启转发'), (102, '关闭转发'),
                              (103, '查看运行状态'), (106, '更改联系人'),
                              (107, '短信筛选设置'),
                              (104, '开机启动设置'), (105, '退出程序')]:
            win32gui.AppendMenu(menu, win32con.MF_STRING, number, label)
        try:
            win32gui.SetForegroundWindow(self.hwnd)
            x, y = win32gui.GetCursorPos()
            win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON, x, y, 0, self.hwnd, None)
            win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(menu)

    def autostart(self):
        executable = str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
        script = str(PROJECT/'set_tray_autostart.ps1')
        def task(mode):
            return subprocess.run([executable, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, '-Mode', mode],
                capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        status = task('Status')
        current = '已开启' if status.returncode == 0 and b'Enabled=true' in status.stdout else '未开启'
        answer = win32gui.MessageBox(self.hwnd,
            f'登录后自动启动托盘：{current}\n\n“是”：开启登录启动\n“否”：关闭登录启动\n“取消”：保持原设置\n\n'
            '仅在当前用户登录后的交互式桌面运行；记住上次本人设置的转发状态，首次为关闭。',
            LABEL, win32con.MB_YESNOCANCEL | win32con.MB_ICONQUESTION | win32con.MB_DEFBUTTON3)
        if answer == win32con.IDCANCEL:
            return
        result = task('Enable' if answer == win32con.IDYES else 'Disable')
        if result.returncode:
            self.message('开机启动设置失败，可能受到 Windows 任务计划权限限制。原设置未作为成功记录。', True)
            self.log.info('autostart_setting_failed')
        else:
            self.message('登录启动已开启。' if answer == win32con.IDYES else '登录启动已关闭。')
            self.log.info('autostart_setting_changed; enabled=%s', answer == win32con.IDYES)

    def command(self, number):
        try:
            if number == 100:
                self.message('灰色暂停：已关闭\n绿色箭头：后台监听运行中\n红色感叹号：故障，发送已禁止\n\n'
                             '右键托盘可开启、关闭、查看状态、更改联系人、设置短信筛选和登录启动、退出。\n'
                             '按保存的关键字模式筛选短信，符合条件即转发全文。')
            elif number == 101:
                config = read_json(self.controller.config_path)
                target = config.get('WECHAT_TARGET', '')
                numbers = '任意发送号码（本人已取消号码限制）' if config.get('SMS_ALLOW_ALL_SENDERS') == '1' else '原手机号白名单'
                answer = win32gui.MessageBox(self.hwnd,
                    f'确认开启真实短信自动转发？\n\n目标好友或群聊：{target}\n发件号码：{numbers}\n'
                    f'筛选规则：{describe_filter(config)}\n'
                    '仅接收原配对手机的 Tailscale 来源、验证原 Secret 和时效。\n'
                    '确认后将设置正式发送授权；每条短信仍核验目标备注与会话标题。\n'
                    'Windows 和微信需保持已登录且桌面可交互。\n\n点击“是”表示你明确批准正式开启。',
                    LABEL, win32con.MB_YESNO | win32con.MB_ICONWARNING | win32con.MB_DEFBUTTON2)
                if answer != win32con.IDYES:
                    return
                self.controller.enable(human_confirmed=True)
                if self.controller.status == 'fault':
                    self.message(ERROR_NAMES.get(self.controller.error, self.controller.error), True)
                else:
                    self.message('开启指令已接受。图标转为绿色后表示后台监听就绪；红色表示故障。')
                self.log.info('human_enable; state=%s', self.controller.status)
            elif number == 102:
                self.controller.disable()
                self.log.info('human_disable; sending_revoked')
                self.message('转发已关闭；等待任务已丢弃，后台接收与发送子进程已退出。')
            elif number == 103:
                self.message(self.status_text(), self.controller.status == 'fault')
            elif number == 104:
                self.autostart()
            elif number == 106:
                if getattr(self, 'recipient_editor_open', False) or getattr(self, 'filter_editor_open', False):
                    return
                from recipient_dialog import show_recipient_dialog
                self.recipient_editor_open = True
                try:
                    show_recipient_dialog(self.controller,
                        on_ready=lambda close: setattr(self, 'recipient_editor_close', close))
                finally:
                    self.recipient_editor_open = False
                    self.recipient_editor_close = None
                self.log.info('recipient_editor_closed; no_message_send')
            elif number == 107:
                if getattr(self, 'filter_editor_open', False) or getattr(self, 'recipient_editor_open', False):
                    return
                from filter_dialog import show_filter_dialog
                self.filter_editor_open = True
                try:
                    show_filter_dialog(self.controller,
                        on_ready=lambda close: setattr(self, 'filter_editor_close', close))
                finally:
                    self.filter_editor_open = False
                    self.filter_editor_close = None
                self.log.info('sms_filter_editor_closed; no_message_send')
            elif number == 105:
                self.quit_event.set()
                for field in ('recipient_editor_close', 'filter_editor_close'):
                    close_editor = getattr(self, field, None)
                    if close_editor is not None:
                        try:
                            close_editor()
                        except Exception:
                            self.log.info('settings_editor_close_failed; exit_continues')
                try:
                    self.controller.close()
                except Exception:
                    self.log.info('exit_persistence_failed; live_sending_revoked')
                    self.message('实时发送和子进程已停止，但关闭状态保存失败；下次开启前请检查配置目录。', True)
                finally:
                    self.log.info('human_exit; sending_revoked')
                    win32gui.DestroyWindow(self.hwnd)
                return
            self.notify()
        except Exception as error:
            known = str(error) if str(error) in ERROR_NAMES else 'operation_failed'
            self.log.info('tray_operation_failed; operation=%s; code=%s', number, known)
            if number == 101:
                self.controller.status = 'fault'
                self.controller.error = known if known in ERROR_NAMES else 'controller_failure_manual_review'
                self.controller.publish()
                self.notify()
            self.message(ERROR_NAMES.get(known, '操作失败，请查看运行状态。'), True)

    def monitor(self):
        while not self.quit_event.wait(1):
            try:
                self.controller.tick()
                value = (self.controller.status, self.controller.error)
                if value != self.last_ui:
                    self.log.info('state=%s; reason=%s', *value)
                    self.last_ui = value
                win32gui.PostMessage(self.hwnd, WM_REFRESH, 0, 0)
            except Exception:
                self.controller.backend.stop()
                self.log.info('supervisor_exception_sending_stopped')

    def window_proc(self, hwnd, message, wparam, lparam):
        if message == WM_TRAY:
            if lparam == win32con.WM_RBUTTONUP:
                self.menu()
            elif lparam == win32con.WM_LBUTTONDBLCLK:
                self.command(103)
        elif message == win32con.WM_COMMAND:
            self.command(win32api.LOWORD(wparam))
        elif message == WM_REFRESH:
            self.notify()
        elif message == self.taskbar_restart:
            self.notify(win32gui.NIM_ADD)
        elif message == 0x2B1 and wparam == 7:
            with self.controller.lock:
                if self.controller.state['desired_enabled']:
                    self.controller.suspend('session_locked'); self.controller.publish()
        elif message == win32con.WM_POWERBROADCAST and wparam == 4:
            with self.controller.lock:
                if self.controller.state['desired_enabled']:
                    self.controller.suspend('windows_suspend'); self.controller.publish()
            return 1
        elif message == win32con.WM_QUERYENDSESSION:
            self.quit_event.set()
            self.controller.backend.revoke()
            with self.controller.lock:
                self.controller.backend.stop()  # Preserve last explicit switch state on logoff.
            return 1
        elif message == win32con.WM_ENDSESSION and wparam:
            win32gui.DestroyWindow(hwnd)
        elif message == win32con.WM_DESTROY:
            self.quit_event.set(); self.controller.backend.revoke()
            with self.controller.lock:
                self.controller.backend.stop()
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 1))
            win32gui.PostQuitMessage(0)
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

def main():
    atomic_json(ROOT/'tray-bootstrap.json', {'pid':os.getpid(),'phase':'main'})
    parser = argparse.ArgumentParser()
    parser.add_argument('--status-on-launch', action='store_true')
    args = parser.parse_args()
    if win32ts.ProcessIdToSessionId(os.getpid()) == 0:
        setup_logging().error('session_zero_refused')
        raise SystemExit(1)
    atomic_json(ROOT/'tray-bootstrap.json', {'pid':os.getpid(),'phase':'mutex'})
    mutex = acquire_instance()
    if mutex is None:
        return
    app = None
    try:
        atomic_json(ROOT/'tray-bootstrap.json', {'pid':os.getpid(),'phase':'construct'})
        app = Tray()
        if args.status_on_launch:
            app.command(103)
        win32gui.PumpMessages()
    except Exception:
        setup_logging().error('tray_start_or_message_loop_failed')
        if app is not None:
            app.controller.backend.stop()
        raise
    finally:
        if app is not None:
            app.quit_event.set(); app.controller.backend.stop()
        win32event.ReleaseMutex(mutex); mutex.Close()

if __name__ == '__main__':
    main()
