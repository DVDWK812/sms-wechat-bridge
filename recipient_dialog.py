"""Local tray recipient editor. Saving/verification never enables sending."""
import queue
import threading


def show_recipient_dialog(controller, on_ready=None):
    import tkinter as tk
    from tkinter import ttk
    from tray_control import read_json

    window = tk.Tk()
    window.title('更改微信联系人或群聊')
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=20)
    frame.grid()
    ttk.Label(frame, text='好友完整备注名或群聊完整名称', font=('Microsoft YaHei UI', 11)).grid(
        row=0, column=0, columnspan=3, sticky='w')
    name = tk.StringVar(value=read_json(controller.config_path).get('WECHAT_TARGET', ''))
    entry = ttk.Entry(frame, textvariable=name, width=38)
    entry.grid(row=1, column=0, columnspan=3, pady=(10, 12), sticky='ew')
    ttk.Label(frame, text='搜索给定名称，使用第一个好友或群聊结果；目前不支持空格。\n'
              '保存会关闭转发并撤销原目标的开启授权。\n'
              '核验仅搜索首项、回车进入聊天并检查标题，不输入或发送消息。\n'
              '核验通过后，仍须在托盘手动确认开启。', justify='left').grid(
        row=2, column=0, columnspan=3, sticky='w')
    status = tk.StringVar(value='当前转发状态不会因打开此窗口而改变。')
    ttk.Label(frame, textvariable=status, wraplength=430, justify='left').grid(
        row=3, column=0, columnspan=3, sticky='w', pady=(14, 14))
    results = queue.Queue()
    busy = False

    def save():
        try:
            verified = controller.set_recipient(name.get())
            status.set('目标未变，有效核验已保留，转发已关闭。关闭此窗口后可在托盘手动开启。'
                       if verified is True else '联系人已保存，转发已关闭。请核验联系人后再手动开启。')
            return True
        except ValueError:
            status.set('请填写 1–64 字的好友完整备注或群聊名称，不含空格或控制字符。')
        except Exception:
            status.set('保存失败，转发已禁止。请查看托盘运行状态。')
        return False

    def verify():
        nonlocal busy
        if busy or not save():
            return
        busy = True
        revision = controller.recipient_revision
        for widget in (entry, save_button, verify_button):
            widget.configure(state='disabled')
        status.set('正在核验，请保持微信已登录、桌面解锁，暂时不要操作键鼠。不发送消息。')

        def worker():
            try:
                controller.verify_recipient(expected_revision=revision)
                results.put(True)
            except Exception:
                results.put(False)
        threading.Thread(target=worker, name='recipient-identity-check', daemon=True).start()
        window.after(100, poll)

    def poll():
        nonlocal busy
        try:
            success = results.get_nowait()
        except queue.Empty:
            window.after(100, poll)
            return
        busy = False
        for widget in (entry, save_button, verify_button):
            widget.configure(state='normal')
        status.set('目标联系人及会话标题核验通过。转发仍关闭，请关闭此窗口后在托盘手动开启。'
                   if success else '核验未通过，转发保持关闭。请检查备注、微信登录及桌面状态后再手动核验。')
        window.lift()

    def close():
        if busy:
            controller.disable()  # Cancels the owned identity-only worker too.
        window.destroy()

    save_button = ttk.Button(frame, text='保存联系人', command=save)
    save_button.grid(row=4, column=0, padx=(0, 8))
    verify_button = ttk.Button(frame, text='核验联系人（不发送）', command=verify)
    verify_button.grid(row=4, column=1, padx=8)
    ttk.Button(frame, text='关闭窗口', command=close).grid(row=4, column=2, padx=(8, 0))
    window.protocol('WM_DELETE_WINDOW', close)
    if on_ready is not None:
        on_ready(close)
    entry.focus_set()
    window.mainloop()
