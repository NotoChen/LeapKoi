import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Engine"))
import listener_core as core
from configuration import DEFAULTS, data_dir
from lifecycle import startup_decision
from protocol import Events, EventConsole, EventNotifier
from runtime import ApplicationRuntime, PolicyGate


class LifecycleTests(unittest.TestCase):
    def test_leapkoi_data_directory_precedes_legacy_environment(self):
        with patch.dict(os.environ, {"LEAPKOI_DATA_DIR": "/tmp/leapkoi-new",
                                     "APPROVALDESK_DATA_DIR": "/tmp/approvaldesk-old"}):
            self.assertEqual(data_dir(), Path("/tmp/leapkoi-new"))
        with patch.dict(os.environ, {"APPROVALDESK_DATA_DIR": "/tmp/approvaldesk-old"}, clear=False):
            os.environ.pop("LEAPKOI_DATA_DIR", None)
            self.assertEqual(data_dir(), Path("/tmp/approvaldesk-old"))

    def test_retryable_0_2_1_budget(self):
        for retryable, expected in [(False, 0), (True, 2), (None, 1)]:
            decision = startup_decision([json.dumps({"error": {"retryable": retryable}})], 1)
            self.assertEqual(decision.additional_attempts, expected)

    def test_retry_after_and_timestamp_both_are_respected(self):
        decision = startup_decision(['{"error":{"retryable":true,"retry_after_seconds":12,"next_retry_at":"2026-01-01T00:01:00Z"}}'], 1, now=1767225600)
        self.assertEqual(decision.wait_seconds, 60)
        self.assertEqual(startup_decision(["retryable=true retry_after_seconds=15"], 1).wait_seconds, 15)

    def test_protection_is_not_bypassed(self):
        for reason in ["terminal_hold", "in_flight", "cooldown"]:
            self.assertTrue(startup_decision([json.dumps({"reason": reason, "retryable": True})], 1).terminal)
        decision = startup_decision(['{"reason":"cooldown","retryable":true,"retry_after_seconds":20}'], 1)
        self.assertFalse(decision.terminal)
        self.assertEqual(decision.wait_seconds, 20)

    def runtime(self, code):
        settings = dict(DEFAULTS, reconnect_delays=[0], parse_quoted_messages=False)
        output = []
        events = Events(output.append)
        console = EventConsole(events)
        gate = PolicyGate(console, lambda: settings)
        gate.refresh = lambda: False
        dws = Mock()
        dws.consume_command.return_value = [sys.executable, "-B", "-u", "-c", code]
        engine = Mock(active_mrs=0, configured_leader=None)
        group = core.GroupCandidate("审批", "group", 2)
        contact = core.ContactCandidate("成员", "user", "sender", "", "", "")
        runtime = ApplicationRuntime(dws, engine, console, EventNotifier(events, lambda: settings),
                                     {"sender": contact}, group, group, "git2.superboss.cc", gate, threading.Event(),
                                     settings=lambda: settings, events=events)
        return runtime, engine, dws, output

    def test_ready_is_required_before_processing_stdout(self):
        event = {"type": core.EVENT_KEY, "event_id": "event", "message_id": "message", "conversation_id": "group",
                 "sender_open_dingtalk_id": "sender", "content": "https://git2.superboss.cc/erp/core/-/merge_requests/1"}
        code = f"import sys,time\nprint({json.dumps(event)!r}, flush=True)\ntime.sleep(.08)\nprint('[event] ready event_key=test bus_pid=1',file=sys.stderr,flush=True)\ntime.sleep(5)"
        runtime, engine, _, output = self.runtime(code)
        engine.process.side_effect = lambda _: runtime.request_stop()
        runtime.run()
        engine.process.assert_called_once()
        ready_index = next(i for i, e in enumerate(output) if e.get("message") == "钉钉真实订阅已就绪")
        task_index = next(i for i, e in enumerate(output) if e["type"] == "task")
        self.assertLess(ready_index, task_index)

    def test_failed_start_is_bounded_and_never_processes_events(self):
        for retryable, count in [(False, 1), (True, 3), (None, 2)]:
            with self.subTest(retryable=retryable):
                code = f"import sys\nprint({json.dumps({'error': {'retryable': retryable}})!r}, file=sys.stderr)\nsys.exit(1)"
                runtime, engine, dws, _ = self.runtime(code)
                with self.assertRaises(core.AutomationError): runtime.run()
                self.assertEqual(dws.consume_command.call_count, count)
                engine.process.assert_not_called()

    def test_live_mr_limit_does_not_depend_on_previous_task_globals(self):
        saved = core.MAX_URLS
        core.MAX_URLS = 1
        try:
            content = " ".join(f"https://git2.superboss.cc/erp/core/-/merge_requests/{i}" for i in (1, 2))
            refs, _ = core.extract_mr_refs_from_message(content, max_urls=2)
            self.assertEqual(len(refs), 2)
        finally:
            core.MAX_URLS = saved

    def test_idle_bus_is_cleaned_up_after_the_listener_stops(self):
        runtime, _, dws, output = self.runtime("pass")
        dws.recover_stale_event_bus.return_value = (True, "已正常停止旧 DWS 事件总线 4242")
        runtime._cleanup_idle_bus_after_listen()
        dws.recover_stale_event_bus.assert_called_with(timeout=5)
        self.assertTrue(any(event.get("message") == "已正常停止旧 DWS 事件总线 4242" for event in output))


class EventBusRecoveryTests(unittest.TestCase):
    pid = 4242
    client_id = "ding-test-client"

    def payload(self, *, consumers=None, subscriptions=None, holder_pid=None, live_pid=None):
        if consumers is None:
            consumers = []
        if subscriptions is None:
            subscriptions = []
        holder_pid = self.pid if holder_pid is None else holder_pid
        live_pid = self.pid if live_pid is None else live_pid
        return {
            "bus": {
                "entry": {
                    "holder_pid": holder_pid,
                    "state": "running",
                    "meta": {"client_id": self.client_id},
                },
                "live": {
                    "bus": {"pid": live_pid, "client_id": self.client_id},
                    "consumers": consumers,
                },
            },
            "subscriptions": subscriptions,
        }

    def recover(self, payload, *, process_command=None, kill=None):
        status = Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        process = Mock(
            returncode=0,
            stdout=process_command
            or f"/fixture/dws event _bus --client-id {self.client_id}",
            stderr="",
        )
        killer = kill or Mock()
        with patch.object(core.subprocess, "run", side_effect=[status, process]) as run, \
                patch.object(core.os, "kill", killer):
            result = core.DwsClient("/fixture/dws", Mock()).recover_stale_event_bus()
        return result, run, killer

    def test_active_consumer_blocks_bus_termination_even_when_subscriptions_are_empty(self):
        payload = self.payload(consumers=[{"pid": 9001}])
        (recovered, message), run, kill = self.recover(payload)
        self.assertFalse(recovered)
        self.assertIn("1 个活跃消费者", message)
        self.assertEqual(run.call_count, 1)
        kill.assert_not_called()

    def test_active_subscription_still_blocks_bus_termination(self):
        (recovered, message), _run, kill = self.recover(
            self.payload(subscriptions=[{"id": "subscription"}])
        )
        self.assertFalse(recovered)
        self.assertIn("活跃订阅", message)
        kill.assert_not_called()

    def test_unconfirmable_consumer_state_is_not_treated_as_idle(self):
        payload = self.payload()
        payload["bus"]["live"].pop("consumers")
        (recovered, message), _run, kill = self.recover(payload)
        self.assertFalse(recovered)
        self.assertIn("消费者列表", message)
        kill.assert_not_called()

    def test_mismatched_live_pid_blocks_bus_termination(self):
        (recovered, message), _run, kill = self.recover(
            self.payload(live_pid=self.pid + 1)
        )
        self.assertFalse(recovered)
        self.assertIn("PID 不一致", message)
        kill.assert_not_called()

    def test_empty_subscriptions_and_consumers_allow_safe_recovery(self):
        def kill(pid, signum):
            self.assertEqual(pid, self.pid)
            if signum == core.signal.SIGTERM:
                return
            self.assertEqual(signum, 0)
            raise ProcessLookupError

        (recovered, message), _run, killer = self.recover(
            self.payload(),
            kill=Mock(side_effect=kill),
        )
        self.assertTrue(recovered)
        self.assertIn(f"旧 DWS 事件总线 {self.pid}", message)
        self.assertEqual(killer.call_count, 2)

    def test_process_without_matching_client_id_is_not_terminated(self):
        (recovered, message), _run, kill = self.recover(
            self.payload(),
            process_command="/fixture/dws event _bus --client-id another-client",
        )
        self.assertFalse(recovered)
        self.assertIn("client-id 不一致", message)
        kill.assert_not_called()


if __name__ == "__main__": unittest.main()
