"""Managed receiver entry point. Secret stays in process memory/environment."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import os
import sys
import threading
import time

from tray_control import atomic_json


class Event:
    def __init__(self, name):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.OpenEventW.restype = wintypes.HANDLE
        self.handle = self.api.OpenEventW(0x100000, False, name)
        if not self.handle:
            raise RuntimeError('managed_event_unavailable')

    def set(self):
        return self.api.WaitForSingleObject(wintypes.HANDLE(self.handle), 0) == 0

    def close(self):
        self.api.CloseHandle(wintypes.HANDLE(self.handle))


def run():
    ready = Event(os.environ['BRIDGE_TRAY_READY_EVENT'])
    stop = Event(os.environ['BRIDGE_TRAY_STOP_EVENT'])
    gate = Event(os.environ['BRIDGE_TRAY_SEND_EVENT'])
    deadline = time.monotonic()+15
    while not ready.set():
        if stop.set() or time.monotonic() >= deadline:
            return
        time.sleep(.05)
    import bridge
    from wechat_sender import SendResult
    bridge.validate_configuration()
    state = {}
    state_lock = threading.Lock()

    class StatusHandler(bridge.Handler):
        def reply(self, status, message):
            with state_lock:
                state.update(last_time=time.time(), last_result=message)
            super().reply(status, message)

    with bridge.BridgeServer((bridge.BIND, bridge.PORT), StatusHandler) as server:
        # No changes to authentication, extraction or durable fingerprint logic.
        server.send_permitted = gate.set
        original = server.sender.send
        def managed_send(message, *, expires_at=None):
            if not gate.set():
                return SendResult(False, 'policy', 'forwarding_switched_off')
            with state_lock:
                state['active'] = True
            try:
                if os.getenv('BRIDGE_TRAY_MOCK') == '1':
                    # Integration harness only: no import/call of GUI sender.
                    time.sleep(float(os.getenv('BRIDGE_TRAY_MOCK_DELAY', '0')))
                    if not gate.set():
                        return SendResult(False, 'policy', 'forwarding_switched_off')
                    outcome = SendResult(True, 'complete', attempted=True, verified=True)
                else:
                    outcome = original(message, expires_at=expires_at)
                with state_lock:
                    state['last_stage'] = outcome.stage
                return outcome
            finally:
                with state_lock:
                    state['active'] = False
        server.sender.send = managed_send
        listener = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        listener.start()
        try:
            while not stop.set():
                with state_lock:
                    snapshot = dict(state)
                snapshot.update(heartbeat=time.time(), pid=os.getpid(), queue_alive=server.send_thread.is_alive(),
                                halted=server.send_halted.is_set(), pending=server.jobs.unfinished_tasks)
                atomic_json(os.environ['BRIDGE_TRAY_STATUS'], snapshot)
                if not listener.is_alive():
                    raise RuntimeError('http_listener_stopped')
                time.sleep(.2)
        finally:
            server.stop_forwarding()
            server.shutdown()
            listener.join(timeout=1)
    for event in (ready, stop, gate):
        event.close()


if __name__ == '__main__':
    try:
        run()
    except Exception as error:
        # Parent records a fixed process-failure status; no raw input/error logs.
        try:
            atomic_json(os.environ['BRIDGE_TRAY_STATUS'], {'heartbeat':time.time(),
                'fatal':True,'error_class':type(error).__name__})
        except Exception:
            pass
        sys.exit(1)
