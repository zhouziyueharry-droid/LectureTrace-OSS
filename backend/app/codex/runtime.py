"""Codex app-server：stdio 请求/事件路由，不自行实现模型循环。"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path

from .settings import application_dir

# Let the installed Codex runtime choose its native capabilities. Keep only the
# host required for dynamic course tools; do not maintain a blacklist of features.
NATIVE_FEATURES = {'code_mode_host': True}
INTERACTIVE_REQUESTS = {
    'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
    'item/permissions/requestApproval', 'item/tool/requestUserInput',
}


class CodexError(Exception):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


def child_environment(home: Path) -> dict:
    allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'LOCALAPPDATA',
               'APPDATA', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env['CODEX_HOME'] = str(home)
    return env


def executable() -> str:
    found = shutil.which('codex')
    if not found:
        raise CodexError('未找到 Codex CLI，请安装 Codex 后重试')
    return found


def installed_version() -> str:
    try:
        result = subprocess.run([executable(), '--version'], capture_output=True, text=True,
                                timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        version = result.stdout.strip().removeprefix('codex-cli ')
    except (OSError, subprocess.SubprocessError) as e:
        raise CodexError('无法检查 Codex 版本') from e
    if result.returncode != 0 or not version:
        raise CodexError('无法检查 Codex 版本：CLI 未返回有效版本信息')
    return version


class Runtime:
    def __init__(self):
        self.version = installed_version()
        home = application_dir() / 'codex-home'
        home.mkdir(parents=True, exist_ok=True)
        # 只写应用专用配置；凭据由 app-server 管理，应用不读取 auth 文件。
        (home / 'config.toml').write_text(
            'model_provider = "openai"\nforced_login_method = "chatgpt"\n'
            'cli_auth_credentials_store = "file"\nweb_search = "live"\n'
            'approval_policy = "on-request"\napprovals_reviewer = "user"\nsandbox_mode = "workspace-write"\n'
            '[sandbox_workspace_write]\nnetwork_access = true\n'
            '[features]\ncode_mode_host = true\n' +
            ('[windows]\nsandbox = "unelevated"\n' if os.name == 'nt' else ''), encoding='utf-8')
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._subscriptions: dict[str, queue.Queue] = {}
        self._parents: dict[str, str] = {}
        self._closed = False
        self._next_id = 0
        self._login_result = None
        try:
            self.process = subprocess.Popen([executable(), 'app-server', '--stdio', '--strict-config'], cwd=home,
                                            env=child_environment(home), stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            text=True, encoding='utf-8', bufsize=1,
                                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as e:
            raise CodexError('无法启动独立 Codex 运行时') from e
        threading.Thread(target=self._read, daemon=True, name='codex-events').start()
        try:
            self.request('initialize', {'clientInfo': {'name': 'lecturetrace', 'version': '0.2.0'},
                                        'capabilities': {'experimentalApi': True}})
            self._send({'method': 'initialized', 'params': {}})
            self.verify_config()
        except Exception:
            self.close()
            raise

    def _send(self, message: dict) -> None:
        with self._write_lock:
            if self._closed:
                raise CodexError('Codex 进程已退出，请重试')
            try:
                self.process.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
                self.process.stdin.flush()
            except (OSError, ValueError) as e:
                raise CodexError('Codex 连接已断开，请重试') from e

    def request(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        response = queue.Queue()
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = response
        try:
            self._send({'id': request_id, 'method': method, 'params': params or {}})
            message = response.get(timeout=timeout)
            if 'error' in message:
                raise CodexError('Codex 请求失败：' + str(message['error'].get('message', '未知错误')),
                                 code=message['error'].get('code'))
            return message.get('result') or {}
        except queue.Empty as e:
            raise CodexError('Codex 请求超时，请重试') from e
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def subscribe(self, thread_id: str) -> queue.Queue:
        with self._lock:
            if self._closed:
                raise CodexError('Codex 进程已退出，请重试')
            if thread_id in self._subscriptions:
                raise CodexError('这节课正在回答，请等待或停止当前回答')
            channel = queue.Queue()
            self._subscriptions[thread_id] = channel
            return channel

    def unsubscribe(self, thread_id: str) -> None:
        with self._lock:
            self._subscriptions.pop(thread_id, None)

    def respond(self, request_id, result: dict) -> None:
        self._send({'id': request_id, 'result': result})

    def _deny(self, message: dict) -> None:
        method = message['method']
        if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
            result = {'decision': 'decline'}
        elif method == 'item/permissions/requestApproval':
            result = {'permissions': {}, 'scope': 'turn'}
        elif method == 'mcpServer/elicitation/request':
            result = {'action': 'decline', 'content': None}
        elif method == 'item/tool/requestUserInput':
            result = {'answers': {}}
        else:
            self._send({'id': message['id'], 'error': {'code': -32601, 'message': '此能力未开放'}})
            return
        self.respond(message['id'], result)

    def _dispatch(self, message: dict) -> None:
        if 'method' not in message:
            with self._lock:
                destination = self._pending.get(message.get('id'))
            if destination is not None:
                destination.put(message)
            return
        if message['method'] == 'account/login/completed':
            self._login_result = message.get('params')
        thread_id = (message.get('params') or {}).get('threadId')
        with self._lock:
            if message['method'] == 'thread/started':
                thread = (message.get('params') or {}).get('thread') or {}
                source = thread.get('source') or {}
                sub = source.get('subAgent') if isinstance(source, dict) else None
                parent = (sub.get('thread_spawn') or {}).get('parent_thread_id') if isinstance(sub, dict) else None
                if parent:
                    self._parents[thread['id']] = parent
            visited = set()
            while thread_id not in self._subscriptions and thread_id in self._parents and thread_id not in visited:
                visited.add(thread_id)
                thread_id = self._parents[thread_id]
            destination = self._subscriptions.get(thread_id)
        if 'id' in message and (message['method'] not in INTERACTIVE_REQUESTS | {'item/tool/call'} or destination is None):
            self._deny(message)
        elif destination is not None:
            destination.put(message)

    def _read(self) -> None:
        try:
            for line in self.process.stdout:
                if line.strip():
                    self._dispatch(json.loads(line))
        except (OSError, ValueError, TypeError, AttributeError, CodexError):
            pass
        finally:
            self._fail('Codex 进程已退出，请重试')

    def _fail(self, reason: str) -> None:
        with self._lock:
            self._closed = True
            for destination in self._pending.values():
                destination.put({'error': {'message': reason}})
            for destination in self._subscriptions.values():
                destination.put({'method': 'runtime/closed', 'params': {'error': reason}})

    def close(self) -> None:
        self._fail('Codex 运行时已关闭')
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()

    def verify_config(self) -> None:
        """Verify effective capabilities independently of the CLI version."""
        config = self.request('config/read', {'includeLayers': False}).get('config', {})
        expected = {'model_provider': 'openai', 'forced_login_method': 'chatgpt',
                    'approval_policy': 'on-request', 'approvals_reviewer': 'user',
                    'sandbox_mode': 'workspace-write', 'web_search': 'live'}
        mismatches = [k for k, value in expected.items() if config.get(k) != value]
        if mismatches:
            raise CodexError('Codex 安全配置未生效：' + '、'.join(mismatches) + '；已拒绝启用问答')
        feature_response = self.request('experimentalFeature/list', {'limit': 1000})
        features = {item['name']: item['enabled'] for item in feature_response.get('data', [])}
        if features.get('code_mode_host') is not True:
            raise CodexError('Codex 动态工具 host 未启用，请更新 Codex CLI')

    def _skills(self):
        rows = self.request('skills/list', {'forceReload': True}).get('data')
        if (not isinstance(rows, list) or any(not isinstance(row, dict) or row.get('errors') != []
                                             or not isinstance(row.get('skills'), list) for row in rows)):
            raise CodexError('无法核验 Codex 技能目录，已拒绝启用问答')
        skills = [skill for row in rows for skill in row['skills']]
        if any(not isinstance(skill, dict) or type(skill.get('enabled')) is not bool
               or not isinstance(skill.get('path'), str) for skill in skills):
            raise CodexError('无法核验 Codex 技能目录，已拒绝启用问答')
        return skills


_runtime: Runtime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> Runtime:
    global _runtime
    with _runtime_lock:
        if _runtime is None or _runtime._closed:
            if _runtime is not None:
                _runtime.close()
            _runtime = Runtime()
        return _runtime


def close_runtime() -> None:
    global _runtime
    with _runtime_lock:
        if _runtime is not None:
            _runtime.close()
            _runtime = None


def model_choices() -> list[dict]:
    client = get_runtime()
    rows, cursor = [], None
    while True:
        response = client.request('model/list', {'includeHidden': False, **({'cursor': cursor} if cursor else {})})
        rows.extend({'id': item['model'], 'display_name': item.get('displayName') or item['model'],
                     'efforts': [option['reasoningEffort'] for option in item.get('supportedReasoningEfforts', [])],
                     'default_effort': item.get('defaultReasoningEffort')}
                    for item in response.get('data', []))
        cursor = response.get('nextCursor')
        if not cursor:
            return rows


def thread_config(*, resume: bool = False) -> dict:
    """Omitting environments selects Codex's native execution environment."""
    return {'sandbox': 'workspace-write', 'approvalPolicy': 'on-request', 'approvalsReviewer': 'user',
            'config': {'web_search': 'live', 'features': NATIVE_FEATURES,
                       'sandbox_workspace_write': {'network_access': True},
                       **({'windows': {'sandbox': 'unelevated'}} if os.name == 'nt' else {})}}


def verify_thread(response: dict) -> None:
    """Native tools are available while writes stay sandboxed and escalations ask."""
    sandbox = response.get('sandbox') or {}
    if (not (response.get('thread') or {}).get('environments')
            or response.get('approvalPolicy') != 'on-request'
            or response.get('approvalsReviewer') != 'user'
            or sandbox.get('type') != 'workspaceWrite'
            or response.get('modelProvider') != 'openai'):
        raise CodexError('Codex 原生环境或写入保护未确认，已拒绝启动问答')
