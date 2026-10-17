"""Tray lifecycle only; the existing HTTP/queue/OCR pipeline stays in bridge.py.

Only the tray's explicit human confirmation can grant production permission.
No SMS, secret, or GUI text is stored in controller state or logs.
"""
from __future__ import annotations

import hashlib
import contextlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
from pathlib import Path
import subprocess
import sqlite3
import threading
import time
import uuid
from sms_filter import FILTER_FIELDS, filter_settings, filter_environment, validate_filter

from app_paths import PROJECT, ROOT, VERSION, MODEL_ROOT
CHAT_TYPES = {'friend', 'group', 'chat'}
SELECTION_POLICY = 'unambiguous_exact_chat_v1'
FLAGS_OFF = {'SMS_DRY_RUN': '1', 'SMS_REAL_SEND_APPROVED': '0', 'WECHAT_TARGET_VERIFIED': '0'}
FLAGS_ON = {'SMS_DRY_RUN': '0', 'SMS_REAL_SEND_APPROVED': '1', 'WECHAT_TARGET_VERIFIED': '1'}
SCOPE_FIELDS = ('WECHAT_TARGET', 'WECHAT_TARGET_TYPE', 'SMS_BIND', 'SMS_PORT',
                'SMS_ALLOWED_CLIENT_IPS', 'SMS_ALLOWED_SENDERS', 'SMS_ALLOW_ALL_SENDERS') + FILTER_FIELDS


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers/AV can temporarily deny FILE_SHARE_DELETE. A
        # heartbeat write must not kill the receiver during a status read.
        deadline = time.monotonic() + 1.0
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError as error:
                if getattr(error, 'winerror', None) not in (5, 32, 33) or time.monotonic() >= deadline:
                    raise
                time.sleep(.02)
    finally:
        temporary.unlink(missing_ok=True)


def scope(config):
    data = {key: config.get(key, '') for key in SCOPE_FIELDS}
    data['client_version'] = VERSION
    data.update(filter_environment(config))
    data['message_policy'] = 'configurable_keyword_full_text_v1'
    data['selection_policy'] = SELECTION_POLICY
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def approved(config, state):
    return (all(config.get(key) == value for key, value in FLAGS_ON.items())
            and state.get('approved_scope') == scope(config)
            and bool(state.get('human_approved_at'))
            and config.get('WECHAT_TARGET_TYPE') in CHAT_TYPES)


def friend_proof(root, config):
    """Compatibility name: identity proof for either a friend or a group."""
    for name in ('ocr-acceptance-friend.json', 'tray-recipient-proof.json'):
        try:
            proof = read_json(Path(root) / name)
            matches = (proof.get('recipient') == config.get('WECHAT_TARGET')
                       and proof.get('client_version') == VERSION
                       and proof.get('success') is True and proof.get('verified') is True)
            if name == 'ocr-acceptance-friend.json':
                if matches and proof.get('attempted') is True and config.get('WECHAT_TARGET_TYPE') == 'friend':
                    return True
            elif matches and proof.get('identity_only') is True:
                old_friend = proof.get('schema') == 'recipient_identity_v1' and config.get('WECHAT_TARGET_TYPE') == 'friend'
                current_chat = (proof.get('schema') == 'recipient_identity_v2'
                    and proof.get('target_type') == config.get('WECHAT_TARGET_TYPE')
                    and proof.get('target_type') in CHAT_TYPES and proof.get('selection_policy') == SELECTION_POLICY)
                if (old_friend or current_chat) and proof.get('dry_run') is True and proof.get('attempted') is False:
                    return True
        except Exception:
            pass
    return False


def validate_recipient(recipient):
    # Match the existing sender's exact-remark contract, never normalize a
    # supplied name into a different contact or use the current chat instead.
    if (not isinstance(recipient, str) or not recipient or len(recipient) > 64
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in recipient)
            or recipient == '文件传输助手'):
        raise ValueError('invalid_friend_remark')
    return recipient


class RecipientVerifier:
    """A cancellable, job-owned identity-only invocation of the SAME sender."""
    def __init__(self, root=ROOT, target_type='chat'):
        self.root = Path(root)
        if target_type not in CHAT_TYPES:
            raise ValueError('invalid_target_type')
        self.target_type = target_type
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.process = self.job = None

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            if self.job is not None:
                self.job.Close()
                self.job = None

    def __call__(self, recipient):
        from wechat_sender import SendResult, STAGES
        import win32job
        process = None
        try:
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('SMS_', 'WECHAT_', 'BRIDGE_TRAY_'))}
            env['SMS_WECHAT_DATA_DIR'] = str(self.root)
            env['SMS_OCR_MODEL_DIR'] = str(MODEL_ROOT)
            with self.lock:
                if self.cancelled.is_set():
                    return SendResult(False, 'policy', 'recipient_verification_cancelled', dry_run=True)
                self.job = win32job.CreateJobObject(None, 'Local\\SmsWechatBridge-Identity-' + uuid.uuid4().hex)
                limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
                limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
                self.process = process = subprocess.Popen(
                    [str(PROJECT/'.venv-wechat/Scripts/python.exe'), '-X', 'utf8', str(PROJECT/'wechat_sender.py'), '--worker'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding='utf-8', env=env, cwd=PROJECT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                win32job.AssignProcessToJobObject(self.job, int(process._handle))
            # Fixed harmless placeholder; dry_run returns before editor input.
            output, _ = process.communicate(json.dumps({'recipient': recipient, 'text': '联系人核验',
                                                       'dry_run': True, 'approved': False,
                                                       'target_type': self.target_type}), timeout=45)
            if self.cancelled.is_set():
                return SendResult(False, 'policy', 'recipient_verification_cancelled', dry_run=True)
            value = json.loads(output)
            result = SendResult(**value)
            if (process.returncode != 0 or result.success is not True or result.verified is not True
                    or result.dry_run is not True or result.attempted is not False or result.stage != 'complete'):
                if (result.success is False and result.dry_run is True and result.attempted is False
                        and result.stage in STAGES and re.fullmatch(r'[a-z0-9_]{1,100}', result.error or '')):
                    return result  # Fixed diagnostic code only, no OCR/window text.
                return SendResult(False, 'identity', 'recipient_identity_not_verified', dry_run=True)
            return result
        except Exception as error:
            name = type(error).__name__.lower()
            if name not in {'typeerror','valueerror','timeoutexpired','jsondecodeerror',
                            'filenotfounderror','permissionerror','oserror','error'}:
                name = 'failure'
            return SendResult(False, 'worker', 'recipient_identity_worker_' + name, dry_run=True)
        finally:
            self.cancel()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate()


def health_check(config):
    """Read-only checks; never activate WeChat or perform OCR/typing."""
    from wechat_sender import NativeDesktop, SenderError
    try:
        desktop = NativeDesktop()
        if not desktop.gui.IsWindowEnabled(desktop.hwnd):
            return 'wechat_dialog_blocking'
    except SenderError as error:
        return error.error
    except Exception:
        return 'desktop_health_unavailable'
    executable = Path(os.environ.get('TAILSCALE_EXE') or Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Tailscale/tailscale.exe')
    try:
        result = subprocess.run([str(executable), 'status', '--json'], capture_output=True,
            timeout=4, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        data = json.loads(result.stdout)
        if result.returncode or data.get('BackendState') != 'Running':
            return 'tailscale_not_running'
        if config.get('SMS_BIND') not in data.get('Self', {}).get('TailscaleIPs', []):
            return 'tailscale_address_changed'
        online = {ip for peer in (data.get('Peer') or {}).values() if peer.get('Online')
                  for ip in peer.get('TailscaleIPs', [])}
        required = set(str(config.get('SMS_ALLOWED_CLIENT_IPS', '')).split(','))
        if not required or not required <= online:
            return 'paired_phone_offline'
    except Exception:
        return 'tailscale_health_unavailable'
    return ''


class WindowsBackend:
    """One Job Object owns receiver + every OCR subprocess, including wrappers."""
    def __init__(self, root=ROOT, project=PROJECT, mock=False, mock_delay=0):
        import win32event, win32job
        self.event_api, self.job_api = win32event, win32job
        self.root, self.project, self.mock = Path(root), Path(project), mock
        self.mock_delay = mock_delay
        self.process = self.job = self.gate = self.stop_event = self.ready = None
        self.status_path = None
        self.db_path = self.root / ('mock-fingerprints.sqlite3' if mock else 'sent-fingerprints.sqlite3')
        self.revoked = threading.Event()

    def revoke(self, permanent=True):
        if permanent:
            self.revoked.set()
        handle = self.gate
        if handle is not None:
            try:
                self.event_api.ResetEvent(handle)
            except Exception:
                pass  # A concurrent close has already removed permission.

    def permit(self):
        self.revoked.clear()

    def start(self, config):
        import win32crypt
        if self.process is not None:
            raise RuntimeError('receiver_already_owned')
        filter_env = filter_environment(config)
        token = uuid.uuid4().hex
        names = {key: 'Local\\SmsWechatBridge-' + token + '-' + key for key in ('gate', 'stop', 'ready')}
        self.gate = self.event_api.CreateEvent(None, True, False, names['gate'])
        self.stop_event = self.event_api.CreateEvent(None, True, False, names['stop'])
        self.ready = self.event_api.CreateEvent(None, True, False, names['ready'])
        self.job = self.job_api.CreateJobObject(None, 'Local\\SmsWechatBridgeJob-' + token)
        limits = self.job_api.QueryInformationJobObject(self.job, self.job_api.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] |= self.job_api.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self.job_api.SetInformationJobObject(self.job, self.job_api.JobObjectExtendedLimitInformation, limits)
        self.status_path = self.root / ('tray-worker-' + token + '.json')
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('SMS_', 'WECHAT_', 'BRIDGE_TRAY_'))}
        if not self.mock:
            # Preserve this deployment's isolated model/data directories.
            env['SMS_WECHAT_DATA_DIR'] = str(self.root)
            env['SMS_OCR_MODEL_DIR'] = str(MODEL_ROOT)
        for key in SCOPE_FIELDS + tuple(FLAGS_ON):
            if key in config:
                env[key] = str(config[key])
        env.update(filter_env)
        try:
            secret = ('tray-mock-only-' + 'x'*40 if self.mock else
                      win32crypt.CryptUnprotectData(bytes.fromhex(config['SecretProtected']), None, None, None, 0)[1].decode('utf-16-le'))
            if len(secret) < 32:
                raise ValueError('secret_unavailable')
            env['SMS_SECRET'] = secret
            db_name = ('mock-fingerprints.sqlite3' if self.mock else
                       'dry-run-fingerprints.sqlite3' if config.get('SMS_DRY_RUN') == '1' else 'sent-fingerprints.sqlite3')
            self.db_path = self.root / db_name
            env['SMS_DEDUP_DB'] = str(self.db_path)
            env['BRIDGE_TRAY_SEND_EVENT'] = names['gate']
            env['BRIDGE_TRAY_STOP_EVENT'] = names['stop']
            env['BRIDGE_TRAY_READY_EVENT'] = names['ready']
            env['BRIDGE_TRAY_STATUS'] = str(self.status_path)
            if self.mock:
                if config.get('SMS_BIND') != '127.0.0.1':
                    raise ValueError('mock_requires_isolated_loopback')
                env['SMS_DRY_RUN'] = '1'
                env['BRIDGE_TRAY_MOCK'] = '1'
                env['BRIDGE_TRAY_MOCK_DELAY'] = str(self.mock_delay)
            runtime = self.project / '.venv/Scripts/python.exe'
            self.process = subprocess.Popen([str(runtime), '-X', 'utf8', str(self.project / 'tray_worker.py')],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=self.project, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            # Worker waits at READY before importing bridge/listening. No child
            # or GUI action can race Job Object assignment.
            self.job_api.AssignProcessToJobObject(self.job, int(self.process._handle))
            if not self.revoked.is_set():
                self.event_api.SetEvent(self.gate)
            self.event_api.SetEvent(self.ready)
        except Exception:
            self.stop()
            raise RuntimeError('receiver_start_failed') from None
        finally:
            env.pop('SMS_SECRET', None)
            secret = None

    def snapshot(self):
        if self.process is None:
            return {}
        exit_code = self.process.poll()
        if exit_code is not None:
            value = {'dead': True, 'exit_code': exit_code}
            try:
                error_class = read_json(self.status_path).get('error_class')
                if error_class in ('PermissionError', 'OSError', 'RuntimeError', 'ValueError'):
                    value['error_class'] = error_class
            except Exception:
                pass
            return value
        try:
            value = read_json(self.status_path)
            if time.time() - value.get('heartbeat', 0) > 8:
                return {'stale': True}
            return value
        except Exception:
            return {'starting': True}

    def has_uncertain(self):
        if self.db_path is None or not self.db_path.exists():
            return False
        try:
            with contextlib.closing(sqlite3.connect('file:'+self.db_path.as_posix()+'?mode=ro', uri=True, timeout=.5)) as db:
                found = bool(db.execute('SELECT 1 FROM uncertain LIMIT 1').fetchone())
            return found
        except Exception:
            return True  # Cannot establish safe recovery: require human review.

    def stop(self):
        # Revocation is the first operation, before waiting on any process.
        self.revoke(permanent=False)
        if self.stop_event is not None:
            self.event_api.SetEvent(self.stop_event)
        if self.process is not None:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        if self.job is not None:
            # Closing the job kills receiver wrappers and remaining GUI workers.
            self.job.Close()
        if self.process is not None:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for handle in (self.gate, self.stop_event, self.ready):
            if handle is not None:
                handle.Close()
        self.process = self.job = self.gate = self.stop_event = self.ready = None
        if self.status_path:
            self.status_path.unlink(missing_ok=True)


class Controller:
    def __init__(self, root=ROOT, backend=None, health=health_check, proof=friend_proof):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / 'config.json'
        self.state_path = self.root / 'tray-settings.json'
        self.status_path = self.root / 'tray-status.json'
        self.backend = backend or WindowsBackend(self.root)
        self.health, self.proof = health, proof
        self.lock = threading.RLock()
        self.state = {'desired_enabled': False, 'approved_scope': '', 'human_approved_at': ''}
        self.error = ''
        self.hard_fault = False
        self.retry_at = 0.0
        self.crashes = []
        self.started_at = 0.0
        self.resume_check_needed = True
        self.last = {}
        self.status = 'off'
        self.verifying = False
        self.recipient_verifier = None
        self.recipient_revision = 0
        try:
            previous = read_json(self.status_path)
            self.last = {key: previous[key] for key in ('last_time', 'last_result', 'last_stage') if key in previous}
        except Exception:
            pass
        try:
            if self.state_path.exists():
                self.state.update(read_json(self.state_path))
                if type(self.state.get('desired_enabled')) is not bool:
                    raise ValueError('invalid_switch')
        except Exception:
            self.state['desired_enabled'] = False
            self.error = 'settings_invalid'
        self.persist()

    def persist(self):
        atomic_json(self.state_path, self.state)

    def enable(self, *, human_confirmed=False):
        with self.lock:
            if self.verifying:
                raise RuntimeError('recipient_verification_in_progress')
            config = read_json(self.config_path)
            if human_confirmed is not True:
                raise PermissionError('explicit_tray_confirmation_required')
            filter_settings(config)  # Validate before changing any send flags.
            if config.get('WECHAT_TARGET_TYPE') not in CHAT_TYPES or not self.proof(self.root, config):
                self.status, self.error = 'fault', 'friend_acceptance_missing_or_changed'
                self.publish()
                raise RuntimeError('friend_acceptance_missing_or_changed')
            if self.state['desired_enabled'] and approved(config, self.state) and not self.hard_fault:
                return
            problem = self.health(config)
            if problem:
                self.status, self.error = 'fault', problem
                self.publish()
                raise RuntimeError(problem)
            # Only this explicitly confirmed UI action writes production flags.
            config.update(FLAGS_ON)
            atomic_json(self.config_path, config)
            self.state.update(desired_enabled=True, approved_scope=scope(config), human_approved_at=time.time())
            self.persist()
            self.backend.permit()
            self.hard_fault = False
            self.resume_check_needed = False  # Explicit manual action, never a message retry.
            self.crashes = []
            self.retry_at = 0
            self.tick()

    def disable(self):
        # Never wait on the supervisor's health/IO lock before revocation.
        self.backend.revoke()
        with self.lock:
            self.recipient_revision += 1
            if self.recipient_verifier is not None:
                self.recipient_verifier.cancel()
            self.state['desired_enabled'] = False
            # Disable the live gate even if disk writes/config are unavailable.
            self.backend.stop()
            self.status, self.error, self.hard_fault = 'off', '', False
            try:
                self.persist()
                config = read_json(self.config_path)
                if any(config.get(key) != value for key, value in FLAGS_OFF.items()):
                    config.update(FLAGS_OFF)
                    atomic_json(self.config_path, config)
            except Exception:
                self.status, self.error = 'fault', 'disable_persistence_failed_sending_revoked'
                raise
            finally:
                self.publish()

    def set_sms_filter(self, mode, keyword):
        mode, keyword = validate_filter(mode, keyword)
        self.backend.revoke()
        with self.lock:
            self.disable()
            config = read_json(self.config_path)
            config.update(FLAGS_OFF, SMS_FILTER_MODE=mode, SMS_FILTER_KEYWORD=keyword)
            self.state.update(desired_enabled=False, approved_scope='', human_approved_at='')
            self.persist()
            atomic_json(self.config_path, config)
            # Recipient identity is independent; do not invalidate its proof.
            logging.getLogger('sms-tray').info('sms_filter_saved; mode=%s; forwarding_enabled=False', mode)
            self.publish()

    def set_recipient(self, recipient, target_type='chat'):
        validate_recipient(recipient)
        if target_type not in CHAT_TYPES:
            raise ValueError('invalid_target_type')
        self.backend.revoke()
        with self.lock:
            self.disable()
            config = read_json(self.config_path)
            unchanged = (config.get('WECHAT_TARGET') == recipient
                         and config.get('WECHAT_TARGET_TYPE') == target_type)
            keep_identity = unchanged and friend_proof(self.root, config)
            config.update(FLAGS_OFF, WECHAT_TARGET=recipient, WECHAT_TARGET_TYPE=target_type)
            self.state.update(desired_enabled=False, approved_scope='', human_approved_at='')
            # A crash between writes still leaves OFF and no valid old scope.
            self.persist()
            atomic_json(self.config_path, config)
            if not keep_identity:
                atomic_json(self.root/'tray-recipient-proof.json', {
                    'schema': 'recipient_identity_v2', 'identity_only': True,
                    'target_type': target_type, 'selection_policy': SELECTION_POLICY,
                    'recipient': recipient, 'client_version': VERSION, 'verified': False})
            logging.getLogger('sms-tray').info('recipient_saved; changed=%s; identity_preserved=%s',
                                             not unchanged, keep_identity)
            self.publish()
            return keep_identity

    def verify_recipient(self, verifier=None, *, expected_revision=None):
        with self.lock:
            if expected_revision is not None and expected_revision != self.recipient_revision:
                raise RuntimeError('recipient_verification_cancelled')
            if self.verifying:
                raise RuntimeError('recipient_verification_in_progress')
            self.disable()
            config = read_json(self.config_path)
            recipient = validate_recipient(config.get('WECHAT_TARGET'))
            target_type = config.get('WECHAT_TARGET_TYPE')
            if target_type not in CHAT_TYPES:
                raise ValueError('invalid_target_type')
            atomic_json(self.root/'tray-recipient-proof.json', {
                'schema': 'recipient_identity_v2', 'identity_only': True,
                'target_type': target_type, 'selection_policy': SELECTION_POLICY,
                'recipient': recipient, 'client_version': VERSION, 'verified': False})
            revision = self.recipient_revision
            self.verifying = True
            self.recipient_verifier = probe = verifier or RecipientVerifier(self.root,target_type=target_type)
        try:
            result = probe(recipient)
            with self.lock:
                current = read_json(self.config_path)
                if (self.recipient_revision != revision or current.get('WECHAT_TARGET') != recipient
                        or current.get('WECHAT_TARGET_TYPE') != target_type):
                    raise RuntimeError('recipient_verification_cancelled')
                if (result.success is not True or result.verified is not True or result.dry_run is not True
                        or result.attempted is not False or result.stage != 'complete'):
                    self.status, self.error = 'fault', 'recipient_identity_not_verified'
                    self.publish()
                    raise RuntimeError('recipient_identity_not_verified')
                atomic_json(self.root/'tray-recipient-proof.json', {
                    'schema': 'recipient_identity_v2', 'identity_only': True, 'recipient': recipient,
                    'target_type': target_type, 'selection_policy': SELECTION_POLICY,
                    'client_version': VERSION, 'success': True, 'verified': True,
                    'dry_run': True, 'attempted': False, 'verified_at': time.time()})
                self.status, self.error = 'off', ''
                logging.getLogger('sms-tray').info('recipient_identity_verified; dry_run=True; send_attempted=False')
                self.publish()
                return True
        finally:
            with self.lock:
                self.verifying = False
                if self.recipient_verifier is probe:
                    self.recipient_verifier = None

    def suspend(self, error, hard=False):
        self.backend.stop()
        self.status, self.error = 'fault', error
        self.hard_fault |= hard
        if getattr(self.backend,'has_uncertain',lambda:False)():
            self.hard_fault = True
            self.error = 'incomplete_request_manual_review'

    def tick(self):
        with self.lock:
            try:
                if not self.state['desired_enabled']:
                    self.status = 'fault' if self.error else 'off'
                    self.publish()
                    return
                config = read_json(self.config_path)
                if not approved(config, self.state) or not self.proof(self.root, config):
                    self.suspend('authorization_or_target_changed', hard=True)
                elif self.hard_fault:
                    self.status = 'fault'
                elif self.resume_check_needed and getattr(self.backend,'has_uncertain',lambda:False)():
                    self.suspend('incomplete_request_manual_review',hard=True)
                else:
                    self.resume_check_needed = False
                    problem = self.health(config)
                    if problem:
                        self.suspend(problem)
                    else:
                        snapshot = self.backend.snapshot()
                        if snapshot.get('last_time'):
                            self.last = {key: snapshot.get(key) for key in ('last_time', 'last_result', 'last_stage')}
                        if snapshot.get('halted') or snapshot.get('queue_alive') is False:
                            self.suspend('sender_paused_manual_review', hard=True)
                        elif snapshot.get('dead') or snapshot.get('stale'):
                            if snapshot.get('dead'):
                                logging.getLogger('sms-tray').info('receiver_exit; exit_code=%s; error_class=%s',
                                    snapshot.get('exit_code', 'unknown'), snapshot.get('error_class', 'unknown'))
                            self.backend.stop()
                            now = time.monotonic()
                            self.crashes = [value for value in self.crashes if now-value < 60] + [now]
                            self.retry_at = now + 2**len(self.crashes)
                            self.status, self.error = 'fault', 'receiver_crashed_no_message_retry'
                            if len(self.crashes) >= 3:
                                self.hard_fault = True
                            if getattr(self.backend,'has_uncertain',lambda:False)():
                                self.hard_fault = True
                                self.error = 'incomplete_request_manual_review'
                        elif self.backend.process is None:
                            if time.monotonic() >= self.retry_at:
                                self.backend.start(config)
                                self.started_at = time.monotonic()
                                self.status, self.error = 'starting', ''
                        elif snapshot.get('starting'):
                            self.status = 'starting'
                            if time.monotonic()-self.started_at > 15:
                                self.suspend('receiver_start_timeout', hard=True)
                        else:
                            self.status, self.error = 'running', ''
                self.publish()
            except Exception:
                self.suspend('controller_failure_manual_review', hard=True)
                self.publish()

    def publish(self):
        value = {'status': self.status, 'error': self.error, 'desired_enabled': self.state['desired_enabled'],
                 'pid': os.getpid(), 'heartbeat': time.time(), **self.last,
                 'receiver_pid': self.backend.process.pid if self.backend.process is not None else None}
        atomic_json(self.status_path, value)
        return value

    def close(self):
        self.disable()


def setup_logging(root=ROOT):
    Path(root).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('sms-tray')
    if logger.handlers:
        return logger
    handler = RotatingFileHandler(Path(root)/'tray.log', maxBytes=128*1024, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger
