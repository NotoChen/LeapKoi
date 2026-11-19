"""Fast GitLab REST access for the desktop worker.

The CLI remains the authentication and compatibility fallback, but spawning
`glab` for every read adds seconds to a click-sized workflow. This client uses
the token already managed by glab for the same host and falls back to the CLI
whenever direct HTTP cannot be used safely.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import subprocess
import threading
from typing import Any, Mapping, Sequence
import urllib.parse

import listener_core as core

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
DIRECT_TIMEOUT_LIMIT_SECONDS = 20


class _DirectUnavailable(Exception):
    """Signal that the CLI fallback should handle the current operation."""


class GitLabHTTPClient(core.GlabClient):
    supports_parallel_approval_prefetch = True

    def __init__(
        self,
        executable: str,
        console: core.Console,
        hostname: str,
        *,
        primary_token: str = "",
        fallback_token: str = "",
        stop_event: threading.Event | None = None,
        command_timeout: int = core.COMMAND_TIMEOUT_SECONDS,
        retry_attempts: int = core.RETRY_ATTEMPTS,
        retry_delays: Sequence[int] = core.RETRY_DELAYS_SECONDS,
    ) -> None:
        super().__init__(
            executable,
            console,
            hostname,
            stop_event,
            command_timeout=command_timeout,
            retry_attempts=retry_attempts,
            retry_delays=retry_delays,
        )
        self.primary_token = primary_token.strip()
        self.leader_token = fallback_token.strip()
        self.uses_legacy_fallback_token = False
        self._resolved_primary_token: str | None = None
        self._token_lock = threading.Lock()
        self._direct_disabled = False
        self._connections = threading.local()

    def _token(self, use_leader: bool) -> str:
        if use_leader:
            if self.leader_token:
                return self.leader_token
            raise core.AutomationError("未配置备用审批账号")
        if self.primary_token:
            return self.primary_token
        with self._token_lock:
            if self._resolved_primary_token:
                return self._resolved_primary_token
            for name in ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN"):
                value = os.environ.get(name, "").strip()
                if value:
                    self._resolved_primary_token = value
                    return value
            try:
                completed = subprocess.run(
                    [
                        self.executable,
                        "config",
                        "get",
                        "token",
                        "--host",
                        self.hostname,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise _DirectUnavailable from error
            token = (completed.stdout or "").strip()
            if completed.returncode != 0 or not token:
                raise _DirectUnavailable
            self._resolved_primary_token = token
            self.console.debug("已复用 glab 登录凭据启用快速 GitLab API")
            return token

    def _direct_timeout(self) -> float:
        return float(max(1, min(self.command_timeout, DIRECT_TIMEOUT_LIMIT_SECONDS)))

    def _connection(self) -> http.client.HTTPSConnection:
        connection = getattr(self._connections, "value", None)
        if connection is None:
            connection = http.client.HTTPSConnection(
                self.hostname,
                timeout=self._direct_timeout(),
                context=ssl.create_default_context(),
            )
            self._connections.value = connection
        return connection

    def _drop_connection(self) -> None:
        connection = getattr(self._connections, "value", None)
        self._connections.value = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _perform(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers: Mapping[str, str],
    ) -> tuple[int, bytes, Mapping[str, str]]:
        connection = self._connection()
        connection.request(method, path, body=body, headers=dict(headers))
        response = connection.getresponse()
        status = int(response.status)
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        response_headers = {
            str(key).lower(): str(value)
            for key, value in response.getheaders()
        }
        if len(raw) > MAX_RESPONSE_BYTES:
            # Do not reuse a connection with an unconsumed response body.
            self._drop_connection()
        if response.will_close:
            self._drop_connection()
        return status, raw, response_headers

    def _request_once(
        self,
        method: str,
        endpoint: str,
        description: str,
        *,
        use_leader: bool,
        fields: Mapping[str, str] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        if self._direct_disabled:
            raise _DirectUnavailable
        if self.stop_event is not None and self.stop_event.is_set():
            raise core.ProcessingStopped(f"监听正在停止，未执行{description}")
        token = self._token(use_leader)
        data = urllib.parse.urlencode(fields).encode() if fields else None
        headers = {
            "Accept": "application/json",
            "PRIVATE-TOKEN": token,
            "User-Agent": "ApprovalDesk",
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            status, raw, response_headers = self._perform(
                method,
                f"/api/v4/{endpoint.lstrip('/')}",
                data,
                headers,
            )
        except (ssl.SSLError, ssl.CertificateError) as error:
            self._drop_connection()
            self._direct_disabled = True
            self.console.debug("GitLab 直连 TLS 校验不可用，回退到 glab")
            raise _DirectUnavailable from error
        except (TimeoutError, socket.timeout) as error:
            self._drop_connection()
            raise core.AutomationError(
                f"{description}失败：请求超时",
                str(error),
                retryable=True,
            ) from error
        except (
            OSError,
            http.client.HTTPException,
            ConnectionResetError,
            BrokenPipeError,
        ) as error:
            self._drop_connection()
            raise core.AutomationError(
                f"{description}失败：临时网络故障",
                str(error),
                retryable=True,
            ) from error

        if status < 200 or status >= 300:
            if status == 401 and not self.primary_token:
                with self._token_lock:
                    self._resolved_primary_token = None
            raise self._http_error(
                description,
                status,
                raw.decode("utf-8", "replace"),
            )
        if len(raw) > MAX_RESPONSE_BYTES:
            raise core.AutomationError(f"{description}返回内容过大")
        if not raw.strip():
            return None, response_headers
        try:
            return json.loads(raw), response_headers
        except json.JSONDecodeError as error:
            detail = raw.decode("utf-8", "replace")
            raise core.AutomationError(
                f"{description}返回了无法识别的数据",
                core.compact_error_detail(detail),
            ) from error

    @staticmethod
    def _http_error(
        description: str,
        status: int,
        body: str,
    ) -> core.AutomationError:
        lowered = body.lower()
        if status == 401:
            reason, retryable = "认证失败", False
        elif status == 403:
            reason, retryable = "权限不足", False
        elif status == 404:
            reason, retryable = "MR 不存在或当前账号无权访问", False
        elif status == 405:
            reason, retryable = "合并请求失败", False
        elif status == 409:
            reason, retryable = "请求冲突", False
        elif status == 429:
            reason, retryable = "服务端限流", True
        elif status in (400, 422):
            if "sha" in lowered and any(
                word in lowered for word in ("does not match", "mismatch", "changed")
            ):
                reason = "代码版本已经变化"
            elif "conflict" in lowered or "cannot be merged" in lowered:
                reason = "合并请求存在冲突或当前不可合并"
            else:
                reason = "请求参数或当前状态不合法"
            retryable = False
        elif status >= 500:
            reason, retryable = "GitLab 服务暂时不可用", True
        else:
            reason, retryable = "请求失败", False
        detail = core.compact_error_detail(body)
        message = f"{description}失败：{reason}（HTTP {status}）"
        if detail:
            message += f"；返回信息：{detail}"
        return core.AutomationError(message, detail or f"HTTP {status}", retryable=retryable)

    def _read(
        self,
        method: str,
        endpoint: str,
        description: str,
        *,
        use_leader: bool,
        fields: Mapping[str, str] | None = None,
    ) -> Any:
        def operation() -> Any:
            payload, _headers = self._request_once(
                method,
                endpoint,
                description,
                use_leader=use_leader,
                fields=fields,
            )
            return payload

        if method.upper() == "GET":
            return core.retry_operation(
                operation,
                console=self.console,
                description=description,
                stop_event=self.stop_event,
                attempts=self.retry_attempts,
                retry_delays=self.retry_delays,
            )
        return operation()

    def api(self, endpoint: str, description: str, use_leader: bool = False) -> Any:
        try:
            return self._read(
                "GET",
                endpoint,
                description,
                use_leader=use_leader,
            )
        except _DirectUnavailable:
            return super().api(endpoint, description, use_leader)

    def commit_count(self, ref: core.MRRef, use_leader: bool = False) -> int:
        try:
            count = 0
            page = "1"
            visited: set[str] = set()
            while page:
                if page in visited:
                    raise core.AutomationError(f"{ref.key} 提交分页游标重复")
                visited.add(page)
                description = f"查询 {ref.key} 提交数"
                payload, headers = self._request_once(
                    "GET",
                    f"{ref.api_base}/commits?per_page=100&page={urllib.parse.quote(page)}",
                    description,
                    use_leader=use_leader,
                )
                if not isinstance(payload, list):
                    raise core.AutomationError(f"{ref.key} 提交列表格式异常")
                count += len(payload)
                page = str(headers.get("x-next-page") or "").strip()
            return count
        except _DirectUnavailable:
            return super().commit_count(ref, use_leader)

    def approve(self, ref: core.MRRef, sha: str, use_leader: bool) -> None:
        try:
            self._read(
                "POST",
                f"{ref.api_base}/approve",
                f"审批 {ref.key}",
                use_leader=use_leader,
                fields={"sha": sha},
            )
        except _DirectUnavailable:
            super().approve(ref, sha, use_leader)

    def merge(
        self,
        ref: core.MRRef,
        sha: str,
        use_leader: bool,
    ) -> core.MRDetails | None:
        try:
            payload = self._read(
                "PUT",
                f"{ref.api_base}/merge",
                f"合并 {ref.key}",
                use_leader=use_leader,
                fields={"sha": sha},
            )
        except _DirectUnavailable:
            return super().merge(ref, sha, use_leader)
        if not isinstance(payload, dict):
            return None
        try:
            return self._parse_details(ref, payload)
        except core.AutomationError:
            return None
