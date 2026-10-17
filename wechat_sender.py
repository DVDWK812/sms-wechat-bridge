"""Local Windows OCR sender with private temporary clipboard text input.

The default is a navigation-only dry run. Success after sending means that a
the Send key was issued in the verified target, not a delivery receipt.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass, field, replace
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time

from app_paths import VERSION as SUPPORTED_VERSION, MODEL_ROOT, ROOT
MAX_MESSAGE_CHARS = 8192  # Covers every body within the existing HTTP byte cap.
HELPER = "文件传输助手"
STAGES = {"policy", "desktop", "window", "ocr", "search", "contact", "title",
          "input", "send", "verify", "complete", "worker", "expired"}


@contextlib.contextmanager
def quiet_libraries():
    """Suppress Python and native SDK output in the isolated GUI process."""
    with open(os.devnull,'w') as sink:
        saved=[os.dup(fd) for fd in (1,2)]
        try:
            for fd in (1,2):
                os.dup2(sink.fileno(),fd)
            with contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
                yield
        finally:
            for fd,original in zip((1,2),saved):
                os.dup2(original,fd);os.close(original)


@contextlib.contextmanager
def desktop_mutex():
    if sys.platform!='win32':
        raise SenderError('desktop','windows_required')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.restype=wintypes.HANDLE
    mutex=kernel.CreateMutexW(None,False,'Local\\SmsWechatBridgeOcrDesktop')
    if not mutex:
        raise SenderError('desktop','desktop_mutex_unavailable')
    acquired=False
    try:
        status=kernel.WaitForSingleObject(wintypes.HANDLE(mutex),0)
        if status not in (0,128):
            raise SenderError('desktop','another_sender_owns_desktop')
        acquired=True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(wintypes.HANDLE(mutex))
        kernel.CloseHandle(wintypes.HANDLE(mutex))


@dataclass(frozen=True)
class SendResult:
    success: bool
    stage: str
    error: str = ""
    attempted: bool = False
    verified: bool = False
    dry_run: bool = False
    timings_ms: dict[str, float] = field(default_factory=dict)
    delivery_checked: bool = False


class SenderError(Exception):
    def __init__(self, stage: str, error: str):
        self.stage, self.error = stage, error
        super().__init__(error)


def compact(text: str) -> str:
    # OCR inserts spaces between Chinese characters. Punctuation is preserved.
    return "".join(text.split())


@dataclass(frozen=True)
class Line:
    text: str
    x: float
    y: float
    w: float
    h: float
    confidence: float = 1.0


def chat_name_matches(actual: str, recipient: str, chat_type: str) -> bool:
    value, expected = compact(actual), compact(recipient)
    if value == expected:
        return True
    # WeChat may append a UI member count to a group title/result. Never
    # strip arbitrary suffixes or use a substring/fuzzy name match.
    return (chat_type in {'group', 'chat'} and
            re.fullmatch(re.escape(expected) + r'(?:\([0-9]{1,6}\)|（[0-9]{1,6}）)', value) is not None)


def select_chat(lines: list[Line], recipient: str, target_type: str = 'chat') -> tuple[Line, str]:
    """Validate the first selectable result before Enter opens it.

    The first result must match exactly. Multiple visible matches, including
    a friend/group name collision, stop before Enter or any message input.
    """
    if target_type not in {'friend', 'group', 'chat'}:
        raise SenderError('policy', 'invalid_target_type')
    allowed = ({'联系人', '最常使用'} if target_type == 'friend' else
               {'群聊', '最常使用'} if target_type == 'group' else {'联系人', '群聊', '最常使用'})
    if recipient == HELPER:
        allowed = {'功能'} if target_type != 'group' else set()
    sections = {"功能", "联系人", "群聊", "公众号", "服务号", "聊天记录",
                "聊天", "搜索网络结果", "最近在搜", "企业微信联系人", "收藏", "小程序", "最常使用"}
    section = None
    header_x=None
    first = None
    matches = []
    for line in sorted(lines, key=lambda item: (item.y, item.x)):
        value = compact(line.text)
        if value in sections:
            section = value if value not in allowed or line.confidence>=0.96 else None
            header_x=line.x
        elif value.startswith("更多"):
            continue
        elif header_x is not None and line.x<=header_x+10:
            # Unknown or partly recognized section labels end the preceding
            # section. Never let a failed group label inherit 'contacts'.
            section=None
        elif line.confidence<0.8 and len(value)<=2:
            # Avatar decorations and the small result-information icon are
            # sometimes returned as low-confidence punctuation by OCR.
            continue
        elif section is not None:
            # Enter activates the first result, so a later exact result must
            # never justify selecting an earlier fuzzy or forbidden result.
            kind = 'group' if section == '群聊' else 'friend' if section == '联系人' else target_type
            if first is None:
                if section not in allowed or line.confidence < 0.96 or not chat_name_matches(value, recipient, kind):
                    raise SenderError("contact", "first_search_result_not_exact_chat")
                first = (line, kind)
            if section in {'联系人', '群聊', '最常使用', '功能'} and chat_name_matches(value, recipient, kind):
                matches.append(line)
    if len(matches) > 1:
        raise SenderError("contact", "ambiguous_search_results")
    # A truncated list cannot prove that a hidden result has no same-name chat.
    if first and any(compact(line.text).startswith('更多') for line in lines):
        raise SenderError("contact", "search_results_incomplete")
    if first:
        return first
    raise SenderError("contact", "exact_first_search_result_unconfirmed")


def choose_contact(lines: list[Line], recipient: str, target_type: str = 'chat') -> Line:
    return select_chat(lines, recipient, target_type)[0]


class WindowsOCR:
    def __init__(self):
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        self.engine = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
        if self.engine is None:
            raise SenderError("ocr", "simplified_chinese_ocr_unavailable")

    async def _read(self, image) -> list[Line]:
        from winrt.windows.graphics.imaging import BitmapDecoder, BitmapPixelFormat, BitmapAlphaMode
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
        data = io.BytesIO()
        image.save(data, format="PNG")
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(data.getvalue())
        await writer.store_async()
        await writer.flush_async()
        writer.detach_stream()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_converted_async(BitmapPixelFormat.BGRA8, BitmapAlphaMode.IGNORE)
        result = await self.engine.recognize_async(bitmap)
        lines = []
        for line in result.lines:
            boxes = [word.bounding_rect for word in line.words]
            if boxes:
                x, y = min(b.x for b in boxes), min(b.y for b in boxes)
                right, bottom = max(b.x+b.width for b in boxes), max(b.y+b.height for b in boxes)
                lines.append(Line(line.text, x, y, right-x, bottom-y))
        bitmap.close()
        stream.close()
        return lines

    def read(self, image) -> list[Line]:
        from PIL import ImageOps
        # Small pale placeholder text needs contrast and padding. This stays
        # local; compensate coordinates back into the original screenshot.
        enlarged = ImageOps.autocontrast(image.convert("L")).convert("RGB").resize((image.width*2,image.height*2))
        enlarged = ImageOps.expand(enlarged,border=20,fill="white")
        async def bounded():
            return await asyncio.wait_for(self._read(enlarged), timeout=5)
        return [Line(v.text,(v.x-20)/2,(v.y-20)/2,v.w/2,v.h/2) for v in asyncio.run(bounded())]


class PaddleLocalOCR:
    def __init__(self):
        started=time.perf_counter()
        self.total_ms=0.0
        self.calls=0
        # Bootstrap downloads official model weights separately. Production
        # receives explicit local paths and does not download or upload images.
        # Paddle's Windows C++ loader cannot reliably open Chinese paths.
        model_root=MODEL_ROOT
        det,rec=model_root/'PP-OCRv5_mobile_det',model_root/'PP-OCRv5_mobile_rec'
        if not all((path/'inference.json').is_file() for path in (det,rec)):
            raise SenderError('ocr','local_paddle_models_missing')
        os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']='True'
        from paddleocr import PaddleOCR
        self.engine=PaddleOCR(text_detection_model_dir=str(det),text_recognition_model_dir=str(rec),
            text_detection_model_name='PP-OCRv5_mobile_det',text_recognition_model_name='PP-OCRv5_mobile_rec',
            use_doc_orientation_classify=False,use_doc_unwarping=False,use_textline_orientation=False,
            device='cpu',cpu_threads=2,enable_mkldnn=False)
        self.init_ms=(time.perf_counter()-started)*1000

    def read(self,image):
        started=time.perf_counter()
        import numpy as np
        results=list(self.engine.predict(np.asarray(image.convert('RGB'))[:,:,::-1]))
        lines=[]
        for result in results:
            for text,score,box in zip(result['rec_texts'],result['rec_scores'],result['rec_boxes']):
                left,top,right,bottom=map(float,box)
                lines.append(Line(text,left,top,right-left,bottom-top,float(score)))
        self.total_ms+=(time.perf_counter()-started)*1000
        self.calls+=1
        return lines


class NativeDesktop:
    def __init__(self):
        if sys.platform != "win32":
            raise SenderError("desktop", "windows_required")
        import win32gui
        import win32api
        import win32process
        import psutil
        from PIL import Image
        self.gui, self.api, self.proc, self.psutil, self.Image = win32gui, win32api, win32process, psutil, Image
        self.u32 = ctypes.WinDLL("user32", use_last_error=True)
        self.u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        self.u32.OpenInputDesktop.restype = wintypes.HANDLE
        self.u32.GetDpiForWindow.argtypes = [wintypes.HWND]
        self.guard_desktop()
        found = []
        def collect(hwnd, unused):
            try:
                pid = self.proc.GetWindowThreadProcessId(hwnd)[1]
                process = self.psutil.Process(pid)
                rect = self.gui.GetWindowRect(hwnd)
                if (process.name().lower() == "weixin.exe" and
                    self.gui.GetWindowText(hwnd) == "微信" and
                    self.gui.GetClassName(hwnd) == "Qt51514QWindowIcon" and
                    rect[2]-rect[0] > 600 and rect[3]-rect[1] > 400):
                    found.append((hwnd, process.exe(), rect))
            except (self.psutil.Error, OSError):
                return
        self.gui.EnumWindows(collect, None)
        if len(found) != 1:
            raise SenderError("window", "logged_in_main_window_not_unique")
        self.hwnd, executable, self.rect = found[0]
        version = self.api.GetFileVersionInfo(executable, "\\")
        high, low = version["FileVersionMS"], version["FileVersionLS"]
        self.version = f"{high>>16}.{high&65535}.{low>>16}.{low&65535}"
        if self.version != SUPPORTED_VERSION:
            raise SenderError("window", "client_version_requires_new_acceptance")
        self.scale = self.u32.GetDpiForWindow(self.hwnd) / 96
        if not 0.75 <= self.scale <= 2.5:
            raise SenderError("window", "unsupported_display_scale")
        self.width, self.height = self.rect[2]-self.rect[0], self.rect[3]-self.rect[1]
        self.activation_title_click=False

    def guard_desktop(self):
        # Tray revokes this per-receiver lease before shutting down children.
        # Existing manual/acceptance senders have no lease and are unchanged.
        name = os.environ.get('BRIDGE_TRAY_SEND_EVENT')
        if name:
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.OpenEventW.restype = wintypes.HANDLE
            handle = kernel.OpenEventW(0x100000, False, name)
            if not handle:
                raise SenderError('policy', 'forwarding_switched_off')
            try:
                if kernel.WaitForSingleObject(wintypes.HANDLE(handle), 0) != 0:
                    raise SenderError('policy', 'forwarding_switched_off')
            finally:
                kernel.CloseHandle(wintypes.HANDLE(handle))
        session = wintypes.DWORD()
        if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) or session.value == 0:
            raise SenderError("desktop", "interactive_user_session_required")
        if ctypes.windll.kernel32.WTSGetActiveConsoleSessionId() != session.value:
            raise SenderError("desktop", "active_console_session_required")
        desk = self.u32.OpenInputDesktop(0, False, 1)
        if not desk:
            raise SenderError("desktop", "input_desktop_unavailable")
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            if not self.u32.GetUserObjectInformationW(wintypes.HANDLE(desk), 2, name, ctypes.sizeof(name), ctypes.byref(needed)) or name.value != "Default":
                raise SenderError("desktop", "desktop_locked_or_secure_prompt")
        finally:
            self.u32.CloseDesktop(wintypes.HANDLE(desk))

    def activate(self):
        self.guard_desktop()
        if self.gui.IsIconic(self.hwnd) or not self.gui.IsWindowVisible(self.hwnd):
            self.gui.ShowWindow(self.hwnd, 9)
        if self.gui.GetForegroundWindow() != self.hwnd:
            try:
                self.gui.SetForegroundWindow(self.hwnd)
            except Exception:
                pass
            if self.gui.GetForegroundWindow()!=self.hwnd:
                # A background receiver may not have SetForegroundWindow
                # permission. Use one normal click on this visible title
                # bar, only when the actual hit-test belongs to WeChat.
                self.guard_desktop()
                if not self.gui.IsWindowEnabled(self.hwnd) or self.gui.GetWindowRect(self.hwnd)!=self.rect:
                    raise SenderError('desktop','foreground_activation_failed')
                point=(self.rect[0]+self.width//2,self.rect[1]+round(15*self.scale))
                if self.gui.GetAncestor(self.gui.WindowFromPoint(point),2)!=self.hwnd:
                    raise SenderError('desktop','foreground_activation_failed')
                if not self.u32.SetPhysicalCursorPos(*point):
                    raise SenderError('desktop','foreground_activation_failed')
                self.activation_title_click=True
                self.api.mouse_event(2,0,0)
                time.sleep(.08)
                self.api.mouse_event(4,0,0)
        # Wait for the compositor before trusting a screenshot after focus
        # changes. A frame from another window must not guide navigation.
        ctypes.windll.dwmapi.DwmFlush()
        time.sleep(0.5)
        if self.gui.GetForegroundWindow()!=self.hwnd:
            raise SenderError('desktop','foreground_activation_failed')
        self.guard()

    def guard(self):
        self.guard_desktop()
        if not self.gui.IsWindow(self.hwnd) or self.gui.IsIconic(self.hwnd) or not self.gui.IsWindowVisible(self.hwnd):
            raise SenderError("window", "window_unavailable")
        if self.gui.GetForegroundWindow() != self.hwnd:
            raise SenderError("desktop", "wechat_not_foreground")
        if self.gui.GetWindowRect(self.hwnd) != self.rect:
            raise SenderError("window", "window_geometry_changed")
        # Owned dialogs, menus and overlays must not receive typing by accident.
        if not self.gui.IsWindowEnabled(self.hwnd):
            raise SenderError("window", "main_window_blocked_by_dialog")

    def capture(self):
        import win32ui
        self.guard()
        # Move off the editor without clicking: hovering an empty editor
        # displays WeChat's voice-input hint, which is not a user draft.
        if not self.u32.SetPhysicalCursorPos(self.rect[2]-round(200*self.scale),self.rect[1]+round(15*self.scale)):
            raise SenderError('desktop','physical_cursor_move_failed')
        time.sleep(0.25)
        ctypes.windll.dwmapi.DwmFlush()
        # Capture the selected window itself. Desktop-coordinate crops can
        # read another monitor's frame under mixed DPI; cursor overlays also
        # must not cover a verification digit. Foreground guards still apply.
        handle=self.gui.GetWindowDC(self.hwnd)
        source=win32ui.CreateDCFromHandle(handle)
        memory=source.CreateCompatibleDC();bitmap=win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source,self.width,self.height)
            memory.SelectObject(bitmap)
            if not self.u32.PrintWindow(wintypes.HWND(self.hwnd),wintypes.HDC(memory.GetSafeHdc()),2):
                raise SenderError('window','capture_failed')
            image=self.Image.frombuffer('RGB',(self.width,self.height),bitmap.GetBitmapBits(True),'raw','BGRX',0,1).copy()
        finally:
            self.gui.DeleteObject(bitmap.GetHandle())
            memory.DeleteDC();source.DeleteDC();self.gui.ReleaseDC(self.hwnd,handle)
        self.guard()
        if image.size != (self.width,self.height):
            raise SenderError("window", "capture_geometry_mismatch")
        return image

    def click(self, x, y):
        self.guard()
        screen = (round(self.rect[0]+x), round(self.rect[1]+y))
        root = self.gui.GetAncestor(self.gui.WindowFromPoint(screen), 2)
        owned_search = self.gui.GetWindow(root,4)==self.hwnd and self.gui.GetClassName(root)=="Qt51514QWindowToolSaveBits"
        if root != self.hwnd and not owned_search:
            raise SenderError("desktop", "click_obstructed")
        if not self.u32.SetPhysicalCursorPos(*screen):
            raise SenderError("desktop", "physical_cursor_move_failed")
        self.api.mouse_event(2, 0, 0)
        time.sleep(0.08)
        self.api.mouse_event(4, 0, 0)
        time.sleep(0.2)
        self.guard()

    def key(self, vk):
        self.guard()
        self.api.keybd_event(vk, 0, 0)
        self.api.keybd_event(vk, 0, 2)
        time.sleep(0.1)
        self.guard()

    def select_all(self):
        self.guard()
        self.api.keybd_event(17,0,0)
        try:
            self.key(65)
        finally:
            self.api.keybd_event(17,0,2)

    def search_popup(self):
        import win32ui
        self.guard()
        found=[]
        def collect(hwnd,unused):
            if (self.gui.IsWindowVisible(hwnd) and self.gui.GetWindow(hwnd,4)==self.hwnd
                and self.gui.GetClassName(hwnd)=="Qt51514QWindowToolSaveBits"):
                found.append(hwnd)
        self.gui.EnumWindows(collect,None)
        if len(found)!=1:
            raise SenderError("search","owned_search_popup_not_unique")
        hwnd=found[0]
        rect=self.gui.GetWindowRect(hwnd)
        width,height=rect[2]-rect[0],rect[3]-rect[1]
        dc=self.gui.GetWindowDC(hwnd)
        source=win32ui.CreateDCFromHandle(dc)
        memory=source.CreateCompatibleDC()
        bitmap=win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source,width,height)
            memory.SelectObject(bitmap)
            if not self.u32.PrintWindow(wintypes.HWND(hwnd),wintypes.HDC(memory.GetSafeHdc()),2):
                raise SenderError("search","search_popup_capture_failed")
            image=self.Image.frombuffer("RGB",(width,height),bitmap.GetBitmapBits(True),"raw","BGRX",0,1).copy()
            return image,rect[0]-self.rect[0],rect[1]-self.rect[1]
        finally:
            self.gui.DeleteObject(bitmap.GetHandle())
            memory.DeleteDC(); source.DeleteDC(); self.gui.ReleaseDC(hwnd,dc)

    def type_text(self, text):
        import win32clipboard as clipboard
        self.guard()
        if any(self.u32.GetAsyncKeyState(key)&0x8000 for key in (16,17,18,91,92,231)):
            raise SenderError('input','held_key_before_text_input')
        if not text or any(ord(character)<32 and character not in '\r\n\t' for character in text):
            raise SenderError('input','invalid_text_control_character')
        # Never read or restore the previous clipboard/history. Set all
        # Windows history/cloud exclusions before placing plaintext, in the
        # same OpenClipboard transaction. No global preferences are changed.
        formats=('ExcludeClipboardContentFromMonitorProcessing',
                 'CanIncludeInClipboardHistory','CanUploadToCloudClipboard')
        try:
            clipboard.OpenClipboard(self.hwnd)
            try:
                clipboard.EmptyClipboard()
                for name in formats:
                    fmt=clipboard.RegisterClipboardFormat(name)
                    if not fmt:
                        raise SenderError('input','clipboard_privacy_format_unavailable')
                    clipboard.SetClipboardData(fmt,b'\0'*4)
                    value=clipboard.GetClipboardData(fmt)
                    if not isinstance(value,bytes) or len(value)<4 or value[:4]!=b'\0'*4:
                        raise SenderError('input','clipboard_privacy_flags_unconfirmed')
                clipboard.SetClipboardData(clipboard.CF_UNICODETEXT,text)
            except Exception:
                clipboard.EmptyClipboard()
                raise
            finally:
                clipboard.CloseClipboard()
            # Windows publishes the new clipboard transaction on Close;
            # the sequence read while it is still open is not committed.
            sequence=self.u32.GetClipboardSequenceNumber()
        except SenderError:
            raise
        except Exception as error:
            raise SenderError('input','private_clipboard_setup_failed') from error
        try:
            self.guard()
            if not sequence or self.u32.GetClipboardSequenceNumber()!=sequence:
                raise SenderError('input','clipboard_changed_before_paste')
            self.api.keybd_event(17,0,0)
            try:
                self.key(86)  # One Ctrl+V, never Enter or an automatic retry.
            finally:
                self.api.keybd_event(17,0,2)
            time.sleep(.35)
        finally:
            # Never delete a newer clipboard item supplied by the human.
            if self.u32.GetClipboardSequenceNumber()==sequence:
                try:
                    clipboard.OpenClipboard(self.hwnd)
                    try:
                        if self.u32.GetClipboardSequenceNumber()==sequence:
                            clipboard.EmptyClipboard()
                    finally:
                        clipboard.CloseClipboard()
                except Exception as error:
                    raise SenderError('input','private_clipboard_cleanup_failed_no_send') from error
        self.guard()


class VisualSender:
    def __init__(self, desktop=None, ocr=None):
        self.desktop = desktop or NativeDesktop()
        self.ocr = ocr or PaddleLocalOCR()
        self.attempted = False
        self.selected_chat_type = 'friend'

    def read(self, image, box):
        left, top, right, bottom = (round(v) for v in box)
        return [Line(v.text, v.x+left, v.y+top, v.w, v.h,v.confidence) for v in self.ocr.read(image.crop((left,top,right,bottom)))]

    def title_verified(self, recipient):
        d, s = self.desktop, self.desktop.scale
        lines = self.read(d.capture(), (280*s, 32*s, min(d.width-100*s, 780*s), 78*s))
        ordered = sorted(lines, key=lambda line: line.x)
        kind = self.selected_chat_type
        valid_layout = (len(ordered) == 1 or
            (kind in {'group', 'chat'} and len(ordered) == 2 and
             abs((ordered[0].y+ordered[0].h/2)-(ordered[1].y+ordered[1].h/2)) < max(ordered[0].h,ordered[1].h)))
        if (not valid_layout or any(line.confidence < .96 for line in ordered)
                or not chat_name_matches(''.join(line.text for line in ordered), recipient, kind)):
            raise SenderError("title", "chat_title_not_exact")

    def latest_outgoing(self,image):
        import numpy as np
        d,s=self.desktop,self.desktop.scale
        left=round(280*s);top=round(max(90*s,d.height-380*s))
        right=round(self.editor_right(image)-50*s);bottom=round(d.height-143*s)
        values=np.asarray(image.crop((left,top,right,bottom))).astype('int16')
        r,g,b=values[:,:,0],values[:,:,1],values[:,:,2]
        mask=(r>130)&(r<210)&(g>200)&(g<255)&(b>70)&(b<190)&(g-r>25)
        rows=np.where(mask.sum(axis=1)>8)[0]
        if not len(rows):
            return None
        start=end=int(rows[-1])
        while start>0 and mask[start-1].sum()>8:
            start-=1
        columns=np.where(mask[start:end+1].any(axis=0))[0]
        box=(left+int(columns[0]),top+start,left+int(columns[-1])+1,top+end+1)
        return image.crop(box)

    def editor_right(self,image):
        d,s=self.desktop,self.desktop.scale
        # The optional chat-information panel has a vertical divider through
        # the editor. Detect it instead of assuming all chats have that panel.
        for x in range(round(600*s),d.width-round(30*s)):
            pixels=[image.getpixel((x,round(d.height-y*s))) for y in (110,90,70,60)]
            if all(max(p)-min(p)<8 and 215<=p[0]<=235 for p in pixels):
                return x
        return d.width-18*s

    def editor_box(self,image):
        d,s=self.desktop,self.desktop.scale
        rows=[]
        for row in range(round(d.height-153*s),round(d.height-129*s)):
            pixels=[image.getpixel((round(x*s),row)) for x in (400,500,600)]
            if all(max(p)-min(p)<8 and 215<=p[0]<=235 for p in pixels):
                rows.append(row)
        if not rows:
            raise SenderError('input','input_divider_unconfirmed')
        return (300*s,max(rows)+4*s,self.editor_right(image)-20*s,d.height-60*s)

    def input_empty(self,image,box):
        import numpy as np
        values=np.asarray(image.crop(tuple(map(round,box)))).astype('int16')
        mask=(values.min(axis=2)<230)|((values.max(axis=2)-values.min(axis=2))>15)
        ys,xs=np.where(mask)
        if len(xs)==0:
            return True
        # Allow only a narrow caret at the left edge; emoji/image drafts fail.
        return xs.max()-xs.min()<=3*self.desktop.scale and ys.max()-ys.min()<=30*self.desktop.scale and xs.max()<35*self.desktop.scale

    def open_contact(self, recipient, *, target_type='chat'):
        d, s = self.desktop, self.desktop.scale
        d.activate()
        try:
            popup,offset_x,offset_y=d.search_popup()
        except SenderError as exc:
            if exc.error!='owned_search_popup_not_unique':
                raise
        else:
            # Close only a positively identified search popup. Escape on the
            # main window alone can hide WeChat instead.
            d.key(27)
        image = d.capture()
        search = self.read(image, (70*s, 35*s, 230*s, 72*s))
        bar = [image.getpixel((round(x*s),round(y*s))) for x,y in ((145,45),(190,50),(210,50))]
        background = image.getpixel((round(65*s),round(55*s)))
        visual_bar = all(max(p)-min(p)<10 and min(p)>=245 for p in bar) and 205<=background[0]<=235
        if not any(compact(v.text).endswith("搜索") for v in search) and not visual_bar:
            raise SenderError("search", "search_placeholder_unconfirmed")
        labels=[v for v in search if compact(v.text).endswith("搜索")]
        if labels:
            label=labels[0]
            d.click(label.x+label.w/2,label.y+label.h/2)
        else:
            d.click(145*s,55*s)
        d.select_all()
        d.key(8)
        d.type_text(recipient)
        time.sleep(0.8)
        image = d.capture()
        query = self.read(image, (103*s, 40*s, 213*s, 69*s))
        # Only exact typed-query OCR is trusted when available. Some Qt render
        # surfaces expose the placeholder frame; contact and header checks
        # still remain mandatory, with no current-chat fallback.
        query_value = compact("".join(v.text for v in query))
        if query_value and not query_value.endswith("搜索") and query_value != compact(recipient):
            raise SenderError("search", "search_query_not_exact")
        # Qt uses a separate owned tool window for results. Capture this
        # returned window directly, never infer rows from the chat sidebar.
        popup,offset_x,offset_y=d.search_popup()
        lines=[Line(v.text,v.x+offset_x,v.y+offset_y,v.w,v.h,v.confidence) for v in self.ocr.read(popup)]
        _, self.selected_chat_type = select_chat(lines, recipient, target_type)
        d.key(13)
        time.sleep(0.4)
        self.title_verified(recipient)

    def send(self, recipient, text, *, dry_run, expires_at=None, target_type='chat'):
        started=time.perf_counter()
        self.open_contact(recipient, target_type=target_type)
        navigation_ms=(time.perf_counter()-started)*1000
        if dry_run:
            return SendResult(True, "complete", verified=True, dry_run=True)
        d, s = self.desktop, self.desktop.scale
        image = d.capture()
        # Current layout's input area is anchored to the bottom, not another
        # conversation. Require its toolbar and empty editable region.
        input_box = self.editor_box(image)
        if self.read(image, input_box):
            raise SenderError("input", "existing_draft_or_input_layout_unconfirmed")
        if not self.input_empty(image,input_box):
            raise SenderError("input","nontext_draft_or_obstructed_input")
        # Verify toolbar by locating the fixed horizontal divider immediately
        # above the editor. Theme/layout changes fail closed.
        d.click(400*s, d.height-105*s)
        self.title_verified(recipient)
        if expires_at is not None and time.time()>=expires_at:
            raise SenderError('expired','message_expired_before_input')
        d.type_text(text)
        self.title_verified(recipient)
        if expires_at is not None and time.time()>=expires_at:
            raise SenderError('expired','message_expired_before_send')
        self.attempted = True  # Persist caller's marker BEFORE this key press.
        send_started=time.perf_counter()
        d.key(13)
        time.sleep(0.7)
        # Requested by the human: no draft-text OCR or post-send bubble
        # checks. verified means target identity; delivery_checked is false.
        return SendResult(True, "complete", attempted=True, verified=True,
            timings_ms={'navigation':round(navigation_ms,1),
                        'input_and_checks':round((send_started-started)*1000-navigation_ms,1),
                        'send_and_verify':round((time.perf_counter()-send_started)*1000,1)})


def send_message(recipient: str, text: str, *, dry_run: bool = True, approved: bool = False, expires_at: float | None = None, target_type: str = 'chat') -> SendResult:
    if target_type not in {'friend', 'group', 'chat'}:
        return SendResult(False,'policy','invalid_target_type')
    if not isinstance(recipient,str) or not recipient or any(c.isspace() for c in recipient):
        return SendResult(False,"policy","invalid_recipient")
    if not isinstance(text,str) or not text or len(text)>MAX_MESSAGE_CHARS or any(ord(c)<32 and c not in '\r\n\t' for c in text):
        return SendResult(False,"policy","invalid_message_text")
    if not dry_run and not approved:
        return SendResult(False,"policy","explicit_send_approval_required")
    if expires_at is not None and time.time()>=expires_at:
        return SendResult(False,'expired','message_expired')
    runtime = Path(__file__).parent / ".venv-wechat" / "Scripts" / "python.exe"
    if Path(sys.prefix).name != ".venv-wechat":
        if not runtime.is_file():
            return SendResult(False,"worker","sender_environment_missing")
        try:
            # Message stays in an anonymous pipe; never command args or disk.
            child_env = {k:v for k,v in os.environ.items() if not k.startswith(('SMS_','WECHAT_'))}
            child_env.update(SMS_WECHAT_DATA_DIR=str(ROOT), SMS_OCR_MODEL_DIR=str(MODEL_ROOT))
            data = json.dumps({"recipient":recipient,"text":text,"dry_run":dry_run,"approved":approved,'expires_at':expires_at,'target_type':target_type})
            proc = subprocess.run([str(runtime),str(Path(__file__).resolve()),"--worker"],input=data,
                text=True,encoding="utf-8",stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                timeout=45,env=child_env,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            result = json.loads(proc.stdout)
            if result.get("stage") not in STAGES:
                raise ValueError()
            if any(type(result.get(key)) is not bool for key in ('success','attempted','verified','dry_run')):
                raise ValueError()
            return SendResult(**result)
        except subprocess.TimeoutExpired:
            return SendResult(False,"worker","worker_timeout_no_retry",attempted=True)
        except Exception:
            return SendResult(False,"worker","worker_result_invalid_no_retry",attempted=True)
    sender = None
    worker_started=time.perf_counter()
    def with_timings(result):
        metrics=dict(result.timings_ms)
        metrics['worker_total']=round((time.perf_counter()-worker_started)*1000,1)
        if sender is not None:
            metrics.update(ocr=round(getattr(sender.ocr,'total_ms',0.0),1),
                ocr_model_init=round(getattr(sender.ocr,'init_ms',0.0),1),
                ocr_calls=getattr(sender.ocr,'calls',0),
                activation_title_click=float(getattr(sender.desktop,'activation_title_click',False)))
        return replace(result,timings_ms=metrics)
    try:
        # Suppress library diagnostic streams. Never pass exception messages,
        # recognized UI text or SMS text to stdout/logging.
        with quiet_libraries(),desktop_mutex():
            sender = VisualSender()
            return with_timings(sender.send(recipient,text,dry_run=dry_run,expires_at=expires_at,target_type=target_type))
    except SenderError as exc:
        return with_timings(SendResult(False,exc.stage,exc.error,attempted=bool(sender and sender.attempted),dry_run=dry_run))
    except Exception:
        return with_timings(SendResult(False,"worker","sender_exception_no_retry",attempted=bool(sender and sender.attempted),dry_run=dry_run))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker",action="store_true")
    parser.add_argument("--dry-run",metavar="EXACT_REMARK")
    parser.add_argument("--inspect",action="store_true")
    args = parser.parse_args()
    if args.worker:
        try:
            # Anonymous-pipe JSON can use 12 characters per Unicode codepoint
            # (surrogate escapes); its size is not the HTTP/form byte limit.
            data = json.loads(sys.stdin.read(MAX_MESSAGE_CHARS*12 + 4096))
            result = send_message(**data)
        except Exception:
            result = SendResult(False,"worker","invalid_worker_request")
        print(json.dumps(asdict(result)))
    elif args.inspect:
        d = NativeDesktop()
        d.activate()
        ocr = PaddleLocalOCR()
        crop = d.capture().crop((round(70*d.scale),round(35*d.scale),round(230*d.scale),round(72*d.scale)))
        tokens = ocr.read(crop)
        print(json.dumps({"version":d.version,"width":d.width,"height":d.height,"scale":d.scale,
            "search_placeholder":any(compact(v.text).endswith("搜索") for v in tokens)},ensure_ascii=True))
    elif args.dry_run:
        print(json.dumps(asdict(send_message(args.dry_run,"普通测试消息",dry_run=True))))
    else:
        parser.error("Use --dry-run, --inspect, or the private worker pipe")


if __name__ == "__main__":
    main()
