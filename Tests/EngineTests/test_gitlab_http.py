import http.client
import json
from pathlib import Path
import ssl
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Engine"))
import listener_core as core
from gitlab_http import GitLabHTTPClient


class FakeResponse:
    def __init__(self, payload, headers=None, status=200):
        self.status = status
        self.headers = headers or {}
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.will_close = False

    def getheaders(self):
        return list(self.headers.items())

    def read(self, size=-1):
        if size < 0:
            return self._raw
        return self._raw[:size]


class FakeConnection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False
        self.current = None

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        self.current = result

    def getresponse(self):
        return self.current

    def close(self):
        self.closed = True


class GitLabHTTPTests(unittest.TestCase):
    def client(self, **values):
        return GitLabHTTPClient(
            "glab",
            Mock(),
            "git2.superboss.cc",
            primary_token=values.pop("primary_token", "fixture-token"),
            retry_attempts=values.pop("retry_attempts", 1),
            retry_delays=values.pop("retry_delays", (0,)),
            **values,
        )

    def test_direct_read_uses_the_repository_token_without_spawning_glab(self):
        client = self.client()
        connection = FakeConnection([FakeResponse({"id": 7})])
        with patch.object(client, "_connection", return_value=connection):
            self.assertEqual(client.api("user", "查询当前账号"), {"id": 7})
        method, path, body, headers = connection.requests[0]
        self.assertEqual((method, path, body), ("GET", "/api/v4/user", None))
        self.assertEqual(headers["PRIVATE-TOKEN"], "fixture-token")

    def test_authentication_failure_is_terminal_and_not_retried(self):
        client = self.client(retry_attempts=3)
        connection = FakeConnection([FakeResponse({"message": "401 Unauthorized"}, status=401)])
        with patch.object(client, "_connection", return_value=connection):
            with self.assertRaises(core.AutomationError) as raised:
                client.api("user", "查询当前账号")
        self.assertEqual(len(connection.requests), 1)
        self.assertIn("认证失败", str(raised.exception))
        self.assertFalse(raised.exception.retryable)

    def test_network_failures_retry_with_the_configured_budget(self):
        client = self.client(retry_attempts=2)
        connection = FakeConnection([
            ConnectionResetError("connection reset"),
            ConnectionResetError("connection reset"),
        ])
        with patch.object(client, "_connection", return_value=connection):
            with self.assertRaises(core.AutomationError) as raised:
                client.api("user", "查询当前账号")
        self.assertEqual(len(connection.requests), 2)
        self.assertTrue(raised.exception.retryable)

    def test_glab_token_is_resolved_once_and_never_written_to_logs(self):
        client = self.client(primary_token="")
        completed = subprocess.CompletedProcess([], 0, "fixture-token\n", "")
        connection = FakeConnection([FakeResponse({"id": 1}), FakeResponse({"id": 1})])
        with patch.dict("gitlab_http.os.environ", {
            "GITLAB_TOKEN": "",
            "GITLAB_ACCESS_TOKEN": "",
            "OAUTH_TOKEN": "",
        }), \
             patch("gitlab_http.subprocess.run", return_value=completed) as token_command, \
             patch.object(client, "_connection", return_value=connection):
            self.assertEqual(client.api("user", "第一次"), {"id": 1})
            self.assertEqual(client.api("user", "第二次"), {"id": 1})
        self.assertEqual(token_command.call_count, 1)
        self.assertEqual(len(connection.requests), 2)
        self.assertNotIn("fixture-token", str(client.console.mock_calls))

    def test_missing_token_falls_back_to_the_existing_glab_path(self):
        client = self.client(primary_token="")
        completed = subprocess.CompletedProcess([], 0, "", "")
        sentinel = {"source": "glab"}
        with patch.dict("gitlab_http.os.environ", {
            "GITLAB_TOKEN": "",
            "GITLAB_ACCESS_TOKEN": "",
            "OAUTH_TOKEN": "",
        }), \
             patch("gitlab_http.subprocess.run", return_value=completed), \
             patch.object(core.GlabClient, "api", return_value=sentinel) as fallback:
            self.assertEqual(client.api("user", "查询当前账号"), sentinel)
        fallback.assert_called_once_with("user", "查询当前账号", False)

    def test_tls_failure_falls_back_to_the_cli(self):
        client = self.client()
        sentinel = {"source": "glab"}
        connection = FakeConnection([ssl.SSLError("certificate verify failed")])
        with patch.object(client, "_connection", return_value=connection), \
             patch.object(core.GlabClient, "api", return_value=sentinel) as fallback:
            self.assertEqual(client.api("user", "查询当前账号"), sentinel)
        fallback.assert_called_once()
        self.assertTrue(client._direct_disabled)

    def test_commit_count_paginates_and_merge_returns_the_server_identity(self):
        client = self.client()
        ref = core.parse_mr_url(
            "https://git2.superboss.cc/erp/core/-/merge_requests/7"
        )
        responses = [
            FakeResponse(
                [{"id": "a"}, {"id": "b"}],
                {"X-Next-Page": "2"},
            ),
            FakeResponse([{"id": "c"}]),
        ]
        merge_payload = {
            "state": "merged",
            "sha": "sha-checked",
            "title": "测试修改",
            "author": {"id": 20, "name": "作者", "username": "author"},
            "merge_user": {"id": 30, "name": "王五", "username": "wangwu"},
            "source_branch": "feature/a",
            "target_branch": "main",
            "changes_count": "2",
            "web_url": "https://git2.superboss.cc/erp/core/merge_requests/7",
        }
        connection = FakeConnection(responses + [FakeResponse(merge_payload)])
        with patch.object(client, "_connection", return_value=connection):
            self.assertEqual(client.commit_count(ref), 3)
            details = client.merge(ref, "sha-checked", False)
        self.assertIsNotNone(details)
        assert details is not None
        self.assertEqual((details.merged_by_id, details.merged_by_name), (30, "王五"))
        method, path, body, _headers = connection.requests[-1]
        self.assertEqual(method, "PUT")
        self.assertIn("/merge_requests/7/merge", path)
        self.assertIn(b"sha=sha-checked", body)

    def test_commit_count_accepts_lowercase_pagination_headers(self):
        client = self.client()
        ref = core.parse_mr_url(
            "https://git2.superboss.cc/erp/core/-/merge_requests/8"
        )
        connection = FakeConnection([
            FakeResponse([{"id": "a"}], {"x-next-page": "2"}),
            FakeResponse([{"id": "b"}, {"id": "c"}]),
        ])
        with patch.object(client, "_connection", return_value=connection):
            self.assertEqual(client.commit_count(ref), 3)

    def test_stop_event_prevents_a_new_direct_request(self):
        stop = threading.Event()
        stop.set()
        client = self.client(stop_event=stop)
        with patch.object(client, "_connection") as opened:
            with self.assertRaises(core.ProcessingStopped):
                client.api("user", "查询当前账号")
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
