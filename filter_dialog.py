"""Tray SMS filter settings. Saving never enables forwarding."""
def show_filter_dialog(controller, on_ready=None):
    import tkinter as tk
    from tkinter import ttk
    from tray_control import read_json
    from sms_filter import filter_settings, describe_filter

    config = read_json(controller.config_path)
    saved_mode, saved_keyword = filter_settings(config)
    window = tk.Tk()
    window.title('短信筛选设置')
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=20)
    frame.grid()
    ttk.Label(frame, text='选择短信转发条件', font=('Microsoft YaHei UI', 11)).grid(row=0, column=0, columnspan=2, sticky='w')
    mode = tk.StringVar(value=saved_mode)
    keyword = tk.StringVar(value=saved_keyword)
    status = tk.StringVar(value='当前已保存：' + describe_filter(config))

    def changed():
        entry.configure(state='disabled' if mode.get() == 'all' else 'normal')
        status.set('设置尚未保存。保存后转发会关闭，需在托盘手动开启。')

    for row, (value, label) in enumerate((('contains', '包含指定关键字'),
                                         ('not_contains', '不包含指定关键字'),
                                         ('all', '无关键字限制（转发全部短信）')), start=1):
        ttk.Radiobutton(frame, text=label, value=value, variable=mode, command=changed).grid(
            row=row, column=0, columnspan=2, sticky='w', pady=4)
    ttk.Label(frame, text='关键字：').grid(row=4, column=0, sticky='w', pady=(10, 10))
    entry = ttk.Entry(frame, textvariable=keyword, width=35)
    entry.grid(row=4, column=1, sticky='ew', pady=(10, 10))
    if saved_mode == 'all':
        entry.configure(state='disabled')
    ttk.Label(frame, text='按字面匹配，区分英文大小写；不使用正则。\n'
              '符合条件的短信仍原样转发全文，空正文忽略。\n'
              '“无关键字限制”只取消正文筛选，保留来源、鉴权、时效及去重。\n'
              '保存会关闭转发并丢弃等待任务，不改变聊天目标的核验。', justify='left').grid(
        row=5, column=0, columnspan=2, sticky='w')
    ttk.Label(frame, textvariable=status, wraplength=440, justify='left').grid(
        row=6, column=0, columnspan=2, sticky='w', pady=(14, 14))

    def save():
        try:
            controller.set_sms_filter(mode.get(), keyword.get())
            status.set('已保存，转发已关闭。关闭此窗口后，在托盘手动开启即可使用新规则。')
        except ValueError:
            status.set('包含/不包含模式须填写 1–128 字的非空关键字，不含换行或控制字符。')
        except Exception:
            status.set('保存失败，转发已禁止。请查看托盘运行状态。')

    ttk.Button(frame, text='保存筛选设置', command=save).grid(row=7, column=0, padx=(0, 10))
    ttk.Button(frame, text='关闭窗口', command=window.destroy).grid(row=7, column=1, sticky='e')
    window.protocol('WM_DELETE_WINDOW', window.destroy)
    if on_ready is not None:
        on_ready(window.destroy)
    window.mainloop()
