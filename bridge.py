"""SMS Forwarder -> explicitly approved WeChat chat, on a Windows desktop.

Requires: Python 3.12 and a verified local OCR sender on an interactive
Windows desktop. Bind ONLY to a private tailnet IP.
"""

from __future__ import annotations

import base64
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import os
import queue
import re
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote
from wechat_sender import SendResult, send_message
from sms_filter import matches_sms, validate_filter, DEFAULT_MODE, DEFAULT_KEYWORD


SECRET = os.getenv("SMS_SECRET", "")
SENDERS = {s.strip() for s in os.getenv("SMS_ALLOWED_SENDERS", "").split(",") if s.strip()}
ALLOW_ALL_SENDERS = os.getenv('SMS_ALLOW_ALL_SENDERS','0')=='1'
TARGET = os.getenv("WECHAT_TARGET", "")
TARGET_TYPE = os.getenv("WECHAT_TARGET_TYPE", "friend")
TARGET_VERIFIED = os.getenv("WECHAT_TARGET_VERIFIED", "0") == "1"
REAL_SEND_APPROVED = os.getenv("SMS_REAL_SEND_APPROVED", "0") == "1"
CLIENT_IPS = {s.strip() for s in os.getenv("SMS_ALLOWED_CLIENT_IPS", "").split(",") if s.strip()}
BIND = os.getenv("SMS_BIND", "127.0.0.1")
PORT = int(os.getenv("SMS_PORT", "8765"))
DRY_RUN = os.getenv("SMS_DRY_RUN", "1") == "1"
DB_PATH = os.getenv("SMS_DEDUP_DB", "sent_fingerprints.sqlite3")
FILTER_MODE = os.getenv('SMS_FILTER_MODE', DEFAULT_MODE)
FILTER_KEYWORD = os.getenv('SMS_FILTER_KEYWORD', DEFAULT_KEYWORD)
MAX_REQUEST_SIZE = 8192
MAX_AGE_SECONDS = 120
DEDUP_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 5
SEND_WAIT_SECONDS = 55
QUEUE_CAPACITY = 8
TAILNET = ipaddress.ip_network("100.64.0.0/10")

OTP_KEYWORDS = re.compile(r"验证码|校验码|动态码|一次性密码|\bOTP\b|verification\s*code|security\s*code|\bcode\b", re.I)
AFTER_LABEL = re.compile(r"(?:验证码|校验码|动态码|一次性密码|\bOTP\b|verification\s*code|security\s*code|\bcode\b)\s*(?:为|是|[:：=＃#])?\s*(\d{4,8})(?!\d)", re.I)
ANY_CODE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")


def extract_otp(content: str) -> str | None:
    """Legacy fixed-test validation only; production no longer extracts codes."""
    if not OTP_KEYWORDS.search(content):
        return None
    matches = AFTER_LABEL.findall(content)
    if matches:
        return matches[0] if len(set(matches)) == 1 else None
    # Only accept a leading code if it appears BEFORE the verification keyword.
    # Otherwise an unrelated order number could be mistaken for an OTP.
    codes = list(ANY_CODE.finditer(content))
    keyword = OTP_KEYWORDS.search(content)
    if len(codes) == 1 and keyword and codes[0].start() < keyword.start():
        return codes[0].group(0)
    return None


def forwarding_text(content: str) -> str | None:
    """Forward the complete received body verbatim on the user's keyword."""
    return content if content and matches_sms(content, FILTER_MODE, FILTER_KEYWORD) else None


def sign_for(timestamp: str, secret: str) -> str:
    """SmsForwarder's documented timestamp-only signature convention."""
    payload = f"{timestamp}\n{secret}".encode("utf-8")
    raw = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.b64encode(raw).decode("ascii")


def valid_sign(timestamp: str, received: str, secret: str) -> bool:
    # A form POST already URL-decodes once; unquote supports double encoded %2B.
    try:
        return hmac.compare_digest(sign_for(timestamp, secret), unquote(received))
    except (TypeError, ValueError):
        return False


def validate_configuration() -> None:
    validate_filter(FILTER_MODE, FILTER_KEYWORD)
    if len(SECRET) < 32 or (not ALLOW_ALL_SENDERS and not SENDERS) or not TARGET:
        raise ValueError("Set SMS_SECRET (>=32 chars), SMS_ALLOWED_SENDERS, and WECHAT_TARGET")
    if not ALLOW_ALL_SENDERS and any(not re.fullmatch(r"\+?[0-9]{3,20}", value) for value in SENDERS):
        raise ValueError("Sender allowlist must contain exact telephone numbers, never regexes")
    if os.getenv('SMS_ALLOW_ALL_SENDERS','0') not in {'0','1'}:
        raise ValueError('SMS_ALLOW_ALL_SENDERS must be exactly 0 or 1')
    if os.getenv("SMS_DRY_RUN", "1") not in {"0", "1"}:
        raise ValueError("SMS_DRY_RUN must be exactly 0 or 1")
    address = ipaddress.ip_address(BIND)
    if address.version != 4 or (address not in TAILNET and address != ipaddress.ip_address("127.0.0.1")):
        raise ValueError("Bind only to 127.0.0.1 (local tests) or a Tailscale IPv4")
    if not CLIENT_IPS:
        raise ValueError("Set SMS_ALLOWED_CLIENT_IPS to the phone's exact Tailscale IPv4")
    for value in CLIENT_IPS:
        client = ipaddress.ip_address(value)
        if client not in TAILNET and not (DRY_RUN and BIND == "127.0.0.1" and value == "127.0.0.1"):
            raise ValueError("Allowed client must be an exact Tailscale IPv4")
    if TARGET_TYPE not in {"friend", "group", "chat"}:
        raise ValueError("WECHAT_TARGET_TYPE must be friend, group or chat")
    if not 1 <= PORT <= 65535:
        raise ValueError("Invalid port")
    if not DRY_RUN and (BIND == "127.0.0.1" or not REAL_SEND_APPROVED or not TARGET_VERIFIED):
        raise ValueError("Real sending requires a verified chat title, tailnet and explicit approval")


def fingerprint(sender: str, content: str, secret: str) -> str:
    # A keyed hash keeps the OTP out of the dedup database.
    raw = (sender + "\0" + content).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


class WeChatSender:
    def send(self, message: str, *, expires_at: float | None = None) -> SendResult:
        if DRY_RUN:
            print("[DRY RUN] Valid request; WeChat send skipped (no OTP printed).", flush=True)
            return SendResult(True, "complete", dry_run=True)
        if not REAL_SEND_APPROVED or not TARGET_VERIFIED or TARGET_TYPE not in {'friend','group','chat'}:
            raise RuntimeError("Real sending has not been approved and target has not been verified")
        result = send_message(TARGET, message, dry_run=False, approved=True, expires_at=expires_at,target_type=TARGET_TYPE)
        # Do not forward third-party errors/window text into server logs.
        print(f"[WECHAT] success={result.success}; stage={result.stage}; attempted={result.attempted}", flush=True)
        return result


@dataclass
class SendJob:
    message: str = field(repr=False)
    expires_at: float
    future: Future = field(default_factory=Future, repr=False)


class BridgeServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], handler_type: type[BaseHTTPRequestHandler]):
        super().__init__(address, handler_type)
        self.db_lock = threading.Lock()
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS sent (fingerprint TEXT PRIMARY KEY, sent_at INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS uncertain (fingerprint TEXT PRIMARY KEY, attempted_at INTEGER NOT NULL)")
        self.db.commit()
        self.sender = WeChatSender()
        self.jobs = queue.Queue(maxsize=QUEUE_CAPACITY)
        self.stopping = threading.Event()
        self.send_halted = threading.Event()
        self.send_permitted = lambda: True  # Optional tray lease; legacy path unchanged.
        self.send_thread = threading.Thread(target=self._send_worker, name="wechat-send-queue", daemon=True)
        self.send_thread.start()

    def _send_worker(self) -> None:
        while not self.stopping.is_set() or not self.jobs.empty():
            try:
                job = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if not job.future.set_running_or_notify_cancel():
                    continue
                if self.stopping.is_set() or not self.send_permitted() or time.time() >= job.expires_at:
                    job.future.set_result(SendResult(False, "expired", "queue_expired_or_stopped"))
                    continue
                if self.send_halted.is_set():
                    job.future.set_result(SendResult(False,'desktop','sender_halted_after_failure'))
                    continue
                # UI/COM objects live in the sender subprocess, never HTTP
                # threads. This worker serializes all desktop operations.
                outcome=self.sender.send(job.message,expires_at=job.expires_at)
                requires_gui_proof = not DRY_RUN or (isinstance(outcome,SendResult) and not outcome.dry_run)
                if requires_gui_proof and (not isinstance(outcome,SendResult) or not outcome.success or not outcome.verified or not outcome.attempted or outcome.dry_run):
                    self.send_halted.set()
                    print('[WECHAT] paused=True; manual desktop check and restart required',flush=True)
                job.future.set_result(outcome)
            except Exception as exc:
                if not DRY_RUN:
                    self.send_halted.set()
                job.future.set_exception(exc)
            finally:
                job.message = ""  # No plaintext is persisted or retained for retry.
                self.jobs.task_done()

    def stop_forwarding(self) -> None:
        """Revoke queue admission and discard queued plaintext without retry."""
        self.stopping.set()
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                break
            job.message = ''
            if job.future.set_running_or_notify_cancel():
                job.future.set_result(SendResult(False, 'policy', 'forwarding_switched_off'))
            self.jobs.task_done()

    def server_close(self) -> None:
        if hasattr(self, "stopping"):
            self.stop_forwarding()
        # Wait for bounded HTTP handlers before closing their shared DB.
        super().server_close()
        if hasattr(self, "send_thread"):
            self.send_thread.join(timeout=1)
        if hasattr(self, "db"):
            self.db.close()

    def handle_verified_sms(self, sender: str, content: str, expires_at: float | None = None) -> str:
        message = forwarding_text(content)
        if message is None:
            return "ignored"
        fp = fingerprint(sender, content, SECRET)
        now = int(time.time())
        with self.db_lock:
            self.db.execute("DELETE FROM sent WHERE sent_at < ?", (now - DEDUP_SECONDS,))
            if self.db.execute("SELECT 1 FROM uncertain WHERE fingerprint = ?", (fp,)).fetchone():
                self.db.commit()
                return "uncertain"
            if self.db.execute("SELECT 1 FROM sent WHERE fingerprint = ?", (fp,)).fetchone():
                self.db.commit()
                return "duplicate"
            if self.stopping.is_set() or not self.send_permitted():
                return "uncertain"
            # Durable reservation BEFORE enqueue. Timeout/crash/failed GUI
            # attempts stay blocked across restarts; no automatic retries.
            self.db.execute("INSERT INTO uncertain (fingerprint, attempted_at) VALUES (?, ?)", (fp, now))
            self.db.commit()
        deadline=min(expires_at if expires_at is not None else time.time()+MAX_AGE_SECONDS,time.time()+SEND_WAIT_SECONDS)
        job = SendJob(message,deadline)
        try:
            self.jobs.put_nowait(job)
            result = job.future.result(timeout=SEND_WAIT_SECONDS)
        except (queue.Full, FutureTimeout):
            job.future.cancel()  # Cancels a queued job; never interrupts a send.
            if not DRY_RUN:
                self.send_halted.set()
            return "uncertain"
        if not isinstance(result, SendResult) or not result.success:
            return "uncertain"
        if (not DRY_RUN or not result.dry_run) and (not result.attempted or not result.verified or result.dry_run):
            return "uncertain"
        with self.db_lock:
            self.db.execute("INSERT INTO sent (fingerprint, sent_at) VALUES (?, ?)", (fp, int(time.time())))
            self.db.execute("DELETE FROM uncertain WHERE fingerprint = ?", (fp,))
            self.db.commit()
        return "dry_run" if result.dry_run else "sent"


class Handler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def log_message(self, fmt: str, *args: object) -> None:
        # Avoid exposing OTP or secret in HTTP access logs.
        pass

    def reply(self, status: int, message: str) -> None:
        # All reply messages are fixed server-defined outcomes, never input.
        # Record rejections too, so a failed request is distinguishable from
        # an Android listener that never initiated a request.
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print(f"[HTTP] utc={stamp}; status={status}; result={message}", flush=True)
        body = (message + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass  # Client may have timed out after processing; dedup is durable.

    def do_POST(self) -> None:
        started = time.perf_counter()
        if self.client_address[0] not in CLIENT_IPS:
            return self.reply(403, "source rejected")
        if self.path != "/sms":
            return self.reply(404, "not found")
        if not self.headers.get("Content-Type", "").lower().startswith("application/x-www-form-urlencoded"):
            return self.reply(415, "form required")
        if self.headers.get("Transfer-Encoding"):
            return self.reply(400, "transfer encoding unsupported")
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if not 0 < size <= MAX_REQUEST_SIZE:
                return self.reply(413, "invalid size")
            parsed = parse_qs(self.rfile.read(size).decode("utf-8", errors="strict"), keep_blank_values=True, max_num_fields=12)
            if any(len(values) != 1 for values in parsed.values()):
                return self.reply(400, "duplicate field")
            fields = {k: v[0] for k, v in parsed.items() if len(v) == 1}
            sender = fields["from"]
            content = fields["content"]
            timestamp = fields["timestamp"]
            signature = fields["sign"]
        except (ValueError, UnicodeDecodeError, KeyError):
            return self.reply(400, "invalid form")
        except socket.timeout:
            return self.reply(408, "request timeout")

        if not ALLOW_ALL_SENDERS and sender not in SENDERS:
            return self.reply(403, "sender rejected")
        try:
            stamp = int(timestamp)
        except ValueError:
            return self.reply(400, "invalid timestamp")
        if abs(time.time() * 1000 - stamp) > MAX_AGE_SECONDS * 1000:
            return self.reply(403, "expired")
        if not valid_sign(timestamp, signature, SECRET):
            return self.reply(403, "invalid signature")
        try:
            result = self.server.handle_verified_sms(sender, content, min(stamp/1000+MAX_AGE_SECONDS,time.time()+MAX_AGE_SECONDS))  # type: ignore[attr-defined]
        except Exception as exc:
            # Exception class only; never log SMS or window text.
            print(f"[ERROR] WeChat send failed: {type(exc).__name__}", file=sys.stderr, flush=True)
            return self.reply(409, "send uncertain; manual review required; do not retry")
        if result == "uncertain":
            return self.reply(409, "send uncertain; manual review required; do not retry")
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        print(f"[OK] {result}; processing_ms={elapsed_ms}", flush=True)
        return self.reply(200, result)


def main() -> None:
    try:
        validate_configuration()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    sender_filter='all' if ALLOW_ALL_SENDERS else f'allowlist({len(SENDERS)})'
    print(f"Listening on {BIND}:{PORT}; dry_run={DRY_RUN}; sender_filter={sender_filter}", flush=True)
    print("IMPORTANT: Bind only to localhost or your private Tailscale IP. Never expose to the public Internet.", flush=True)
    with BridgeServer((BIND, PORT), Handler) as server:
        server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
