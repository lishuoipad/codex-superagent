"""Native adapter tests: synthetic stores, strict fake RPC, no real socket or model.

Optional AGENT_RELAY_HOST_TEST_BUILD places all fixtures in a registered build
directory. Response names mirror rust-v0.166.0-alpha.5.1 protocol/v2/thread.rs;
these tests do not establish a real Desktop connection or native acceptance.
"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/codex-superagent/scripts"
sys.path.insert(0, str(SCRIPTS))
import task_runtime
import task_runtime_host as host
from task_runtime import TaskRuntime


class Clock:
    def __init__(self, now=1000.0):
        self.now = now
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


class FakeRpc:
    """Implements only read APIs and a fake turn ACK; all other methods fail."""
    def __init__(self, clock=None, budget_clock=None):
        self.clock = clock or Clock()
        self.budget_clock = budget_clock or self.clock
        self.wall_shift_per_call = 0
        self.timeout = 10
        self.calls = []
        self.loaded_ids = ["router", "worker-1", "worker-2", "worker-3", "worker-4"]
        self.loaded_cursor = None
        self.statuses = {}
        self.goals = {}
        self.queues = {}
        self.terminals = {}
        self.handlers = {}
        self.delay = 0
        self.delays = {}
        self.turn_reply = {"turn": {"id": "fake-turn-id"}}
        self.initialized = False
        self.closed = False

    def call(self, method, params):
        self.calls.append({"method": method, "params": copy.deepcopy(params), "timeout": self.timeout})
        duration = self.delays.get(method, self.delay)
        if duration > self.timeout:
            self.elapse(self.timeout, wall_shift=self.wall_shift_per_call)
            raise TimeoutError("fake RPC budget exceeded")
        self.elapse(duration, wall_shift=self.wall_shift_per_call)
        if method in self.handlers:
            return self.handlers[method](copy.deepcopy(params))
        tid = params.get("threadId")
        if method == "thread/loaded/list":
            return {"data": list(self.loaded_ids), "nextCursor": self.loaded_cursor}
        if method == "thread/read":
            if params.get("includeTurns") is not False:
                raise AssertionError("No history hydration is permitted")
            return {"thread": {"id": tid, "status": self.statuses.get(tid, {"type": "idle"})}, "readState": None}
        if method == "thread/goal/get":
            return {"goal": self.goals.get(tid)}
        if method == "thread/queue/list":
            return copy.deepcopy(self.queues.get(tid, {"data": [], "nextCursor": None}))
        if method == "thread/backgroundTerminals/list":
            return copy.deepcopy(self.terminals.get(tid, {"data": [], "nextCursor": None}))
        if method == "turn/start":
            if set(params) != {"threadId", "clientUserMessageId", "input"}:
                raise AssertionError("Unexpected turn overrides")
            if isinstance(self.turn_reply, Exception):
                raise self.turn_reply
            return copy.deepcopy(self.turn_reply)
        raise AssertionError("Unapproved RPC method: " + method)

    def elapse(self, seconds, wall_shift=0):
        self.clock.advance(seconds + wall_shift)
        if self.budget_clock is not self.clock:
            self.budget_clock.advance(seconds)

    def initialize(self):
        self.initialized = True
    def close(self):
        self.closed = True
    def wait(self, seconds):
        self.elapse(seconds)


class RuntimeHostTests(unittest.TestCase):
    def setUp(self):
        self.build = Path(os.environ.get("AGENT_RELAY_HOST_TEST_BUILD", tempfile.gettempdir())).resolve()
        self.build.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-host-", dir=self.build)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(self.build))
        self.clock = Clock()
        self.budget_clock = Clock(50)
        authority = self.root / "authority.json"
        authority.write_text('{"testOnly":"synthetic architecture tests; no native authority"}', encoding="utf-8")
        self.ref = {"path": str(authority), "sha256": hashlib.sha256(authority.read_bytes()).hexdigest()}
        self.config = {
            "schema": 1, "teamId": "host-test-team", "hostId": "fake-host",
            "router": {"threadId": "router", "title": "Router"}, "maxInUseWorkers": 4,
            "initialDevelopmentEnabled": False, "authorization": self.ref,
            "dbPath": str(self.root / "state.sqlite3"), "registryPath": str(self.root / "registry"),
            "statusPath": str(self.root / "status.json"), "allowedEventSources": ["fixture"],
            "workers": [{"threadId": "worker-1", "title": "Worker 1", "taskBindingId": "task-1", "state": "waiting"}]
        }
        self.rt = TaskRuntime(self.config)
        self.rt.initialize()
        self.rpc = FakeRpc(self.clock, self.budget_clock)
        self.native = host.NativeTaskHost(self.rpc, "fake-host", clock=self.clock, budget_clock=self.budget_clock)
        self.runner = host.RuntimeHost(self.rt, self.native, clock=self.clock, token="host-token", budget_clock=self.budget_clock)
        self.rpc_blocker = patch.object(host, "Rpc", side_effect=AssertionError("Real RPC construction forbidden in tests"))
        self.blocked_rpc = self.rpc_blocker.start()
        self.addCleanup(self.rpc_blocker.stop)

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(self.build))
        self.temp.cleanup()

    def event(self, eid="event-1", task="task-1", kind="followup"):
        return {"eventId": eid, "source": "fixture", "kind": kind, "taskBindingId": task, "summary": "synthetic event"}

    def queue_event(self, **kwargs):
        return self.rt.ingest(self.event(**kwargs), now=self.clock())

    def enable(self):
        self.rt.set_gate(True, "router", self.ref)

    def step(self):
        with patch.object(task_runtime.time, "time", self.clock):
            return self.runner.step()

    def methods(self):
        return [c["method"] for c in self.rpc.calls]

    def turn_calls(self):
        return [c for c in self.rpc.calls if c["method"] == "turn/start"]

    def config_file(self):
        path = self.root / "runtime.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        return path

    def invoke(self, *args):
        output = io.StringIO()
        with patch("sys.stdout", output):
            code = host.main(["--config", str(self.config_file()), *args])
        return code, json.loads(output.getvalue().strip())

    def native_fixture_paths(self):
        binary = self.root / "never-executed-codex.exe"
        socket = self.root / "fake-socket"
        binary.write_bytes(b"not executable")
        socket.write_bytes(b"not a socket")
        return binary, socket

    def test_production_paused_step_uses_zero_native_calls(self):
        self.queue_event()
        self.assertEqual(self.step(), {"status": "paused", "nativeCalls": 0})
        self.assertEqual(self.rpc.calls, [])
        exported = json.loads(Path(self.config["statusPath"]).read_text())
        self.assertFalse(exported["developmentEnabled"])
        self.assertEqual(exported["events"], {"pending": 1})

    def test_paused_cli_once_never_constructs_rpc_or_checks_host(self):
        code, result = self.invoke("--once")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "paused")
        self.blocked_rpc.assert_not_called()

    def test_check_missing_socket_never_spawns_or_resumes(self):
        binary, socket = self.native_fixture_paths()
        socket.unlink()
        code, result = self.invoke("--check", "--codex", str(binary), "--sock", str(socket))
        self.assertEqual(code, 2)
        self.assertEqual(result["nativeCalls"], 0)
        self.assertEqual(result["status"], "unavailable")
        self.blocked_rpc.assert_not_called()
        self.assertFalse(self.rt.status()["developmentEnabled"])

    def test_unloaded_target_is_not_resumed_or_sent(self):
        self.enable(); self.queue_event()
        self.rpc.loaded_ids = ["router"]
        result = self.step()
        self.assertEqual(result["status"], "no_eligible_event")
        self.assertIn("worker-1", result["unavailable"])
        self.assertEqual(self.methods(), ["thread/loaded/list"])
        self.assertEqual(self.rt.status()["events"], {"pending": 1})

    def test_target_absent_from_truncated_loaded_page_is_not_assumed_loaded(self):
        self.enable(); self.queue_event()
        self.rpc.loaded_ids = ["unrelated"]
        self.rpc.loaded_cursor = "more-pages"
        self.step()
        self.assertEqual(self.methods(), ["thread/loaded/list"])
        self.assertEqual(self.turn_calls(), [])

    def test_wrong_native_identity_fails_closed(self):
        self.enable(); self.queue_event()
        self.rpc.handlers["thread/read"] = lambda params: {"thread": {"id": "wrong", "status": {"type": "idle"}}}
        self.step()
        self.assertEqual(self.turn_calls(), [])
        self.assertEqual(self.rt.status()["events"], {"pending": 1})

    def test_paused_or_finished_goal_never_sends(self):
        self.enable(); self.queue_event()
        for state in ("paused", "complete", "blocked", "budget_limited", "unknown"):
            with self.subTest(state=state):
                self.rpc.goals["worker-1"] = {"status": state}
                self.step()
                self.assertEqual(self.turn_calls(), [])
        self.assertEqual(self.rt.status()["events"], {"pending": 1})

    def test_queued_or_background_or_attention_target_never_sends(self):
        self.enable(); self.queue_event()
        variants = [
            ("queues", {"data": [{"id": "queued", "input": [], "clientUserMessageId": "queued-id"}], "nextCursor": None}),
            ("queues", {"data": [], "nextCursor": "next"}),
            ("terminals", {"data": [{"itemId": "item", "processId": "p", "command": "fixture", "cwd": ".", "osPid": None, "cpuPercent": None, "rssKb": None}], "nextCursor": None}),
            ("terminals", {"data": [], "nextCursor": "next"})
        ]
        for field, value in variants:
            with self.subTest(field=field, value=value):
                self.rpc.queues.clear(); self.rpc.terminals.clear()
                getattr(self.rpc, field)["worker-1"] = value
                self.step()
                self.assertEqual(self.turn_calls(), [])
        self.rpc.queues.clear(); self.rpc.terminals.clear()
        self.rpc.statuses["worker-1"] = {"type": "active", "activeFlags": ["waitingOnApproval"]}
        self.step()
        self.assertEqual(self.turn_calls(), [])

    def test_busy_and_unknown_native_status_never_sends(self):
        self.enable(); self.queue_event()
        for status in ("active", "systemError", "notLoaded", "unknown"):
            self.rpc.statuses["worker-1"] = {"type": status}
            self.step()
        self.assertEqual(self.turn_calls(), [])
        self.assertEqual(self.rt.status()["events"], {"pending": 1})

    def test_explicit_worker_pause_survives_enabled_team(self):
        self.enable(); self.queue_event()
        self.rt.worker_state("router", "worker-1", "paused", self.ref)
        self.step()
        self.assertEqual(self.turn_calls(), [])
        self.assertEqual(self.rt.status()["events"], {"pending": 1})

    def test_known_event_goes_direct_to_worker_without_model_or_goal_changes(self):
        self.enable()
        self.rt.subscribe("router", "tool-finished", "task-1")
        event = self.event(kind="background_completed")
        event.pop("taskBindingId"); event["sourceKey"] = "tool-finished"
        self.assertEqual(self.rt.ingest(event, self.clock())["recipient"], "worker-1")
        result = self.step()
        self.assertTrue(result["sent"])
        turn = self.turn_calls()[0]["params"]
        self.assertEqual(turn["threadId"], "worker-1")
        self.assertEqual(set(turn), {"threadId", "clientUserMessageId", "input"})
        envelope = json.loads(turn["input"][0]["text"].split("\n", 1)[1])
        self.assertEqual(envelope["taskBindingId"], "task-1")
        self.assertEqual(envelope["event"]["sourceKey"], "tool-finished")
        allowed = {"thread/loaded/list", "thread/read", "thread/goal/get", "thread/queue/list", "thread/backgroundTerminals/list", "turn/start"}
        self.assertTrue(set(self.methods()) <= allowed)
        self.assertFalse(any(c["params"].get("threadId") == "router" for c in self.rpc.calls))
        self.assertEqual(self.rt.status()["workers"][0]["state"], "waiting")
        self.assertEqual(result["evidence"]["meaning"], "native_accepted_not_task_completed")

    def test_delivered_event_not_sent_again(self):
        self.enable(); self.queue_event()
        self.assertTrue(self.step()["sent"])
        self.assertTrue(self.queue_event()["duplicate"])
        self.step()
        self.assertEqual(len(self.turn_calls()), 1)

    def test_unknown_send_failure_never_retries_or_sends_next_same_owner(self):
        self.enable(); self.queue_event()
        self.rpc.turn_reply = TimeoutError("fake unknown acceptance")
        result = self.step()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.rt.status()["events"], {"unknown": 1})
        self.queue_event(eid="event-2")
        self.clock.advance(5)
        self.step()
        self.assertEqual(len(self.turn_calls()), 1)
        self.assertEqual(self.rt.status()["events"], {"pending": 1, "unknown": 1})

    def test_missing_native_turn_id_is_unknown_not_retried(self):
        self.enable(); self.queue_event()
        self.rpc.turn_reply = {"turn": {}}
        self.assertEqual(self.step()["status"], "unknown")
        self.step()
        self.assertEqual(len(self.turn_calls()), 1)

    def test_final_native_goal_pause_prevents_turn_start(self):
        self.enable(); self.queue_event()
        reads = []
        def goal(params):
            reads.append(params)
            return {"goal": None if len(reads) == 1 else {"status": "paused"}}
        self.rpc.handlers["thread/goal/get"] = goal
        result = self.step()
        self.assertEqual(result["status"], "not_sent")
        self.assertEqual(self.turn_calls(), [])
        self.assertEqual(self.rt.status()["events"], {"pending": 1})

    def test_final_native_busy_status_prevents_turn_start(self):
        self.enable(); self.queue_event()
        reads = []
        def read(params):
            reads.append(params)
            return {"thread": {"id": params["threadId"], "status": {"type": "idle" if len(reads) == 1 else "active"}}}
        self.rpc.handlers["thread/read"] = read
        self.assertEqual(self.step()["status"], "not_sent")
        self.assertEqual(self.turn_calls(), [])

    def test_durable_pause_between_claim_and_dispatch_prevents_final_native_calls(self):
        self.enable(); self.queue_event()
        original = self.rt.claim
        def claim_then_pause(*args):
            result = original(*args)
            self.rt.set_gate(False, "router", self.ref)
            return result
        with patch.object(self.rt, "claim", side_effect=claim_then_pause):
            result = self.step()
        self.assertEqual(result["reason"], "admission_changed")
        self.assertEqual(len(self.rpc.calls), 5)
        self.assertEqual(self.turn_calls(), [])
        self.assertFalse(self.rt.status()["developmentEnabled"])

    def test_another_host_lease_blocks_all_native_calls(self):
        self.enable(); self.queue_event()
        self.rt.acquire_host("another-owner", self.clock())
        self.assertEqual(self.step()["status"], "another_host_owns_lease")
        self.assertEqual(self.rpc.calls, [])

    def test_one_unloaded_owner_does_not_block_another_owner(self):
        self.enable(); self.queue_event()
        self.rt.assign("router", {"threadId": "worker-2", "title": "Second", "taskBindingId": "task-2"}, self.ref)
        self.queue_event(eid="event-2", task="task-2")
        self.rpc.loaded_ids = ["worker-2"]
        result = self.step()
        self.assertTrue(result["sent"])
        self.assertIn("worker-1", result["unavailable"])
        self.assertEqual(self.turn_calls()[0]["params"]["threadId"], "worker-2")

    def test_snapshot_deadline_bounds_total_rpc_time_and_restores_timeout(self):
        self.rpc.delay = 3
        start = self.budget_clock()
        with self.assertRaises(TimeoutError):
            self.native.snapshot("worker-1", deadline=start + 5)
        self.assertLessEqual(self.budget_clock() - start, 5)
        self.assertEqual([c["timeout"] for c in self.rpc.calls], [5, 2])
        self.assertEqual(self.rpc.timeout, 10)
        self.assertEqual(self.turn_calls(), [])

    def test_send_budget_includes_final_snapshot_and_turn_request(self):
        self.rpc.delay = 2
        self.rpc.delays["turn/start"] = 10
        start = self.budget_clock()
        with self.assertRaises(TimeoutError):
            self.native.send({"recipient": "worker-1", "eventId": "event", "taskBindingId": "task-1", "event": self.event()})
        self.assertLessEqual(self.budget_clock() - start, 15)
        self.assertEqual(self.turn_calls()[0]["timeout"], 5)
        self.assertEqual(self.rpc.timeout, 10)

    def test_step_snapshot_budget_is_shared_across_all_targets(self):
        self.enable()
        for index in range(1, 5):
            if index > 1:
                self.rt.assign("router", {"threadId": f"worker-{index}", "title": "Worker", "taskBindingId": f"task-{index}"}, self.ref)
            self.queue_event(eid=f"event-{index}", task=f"task-{index}")
        self.queue_event(eid="router-event", task="", kind="new_request")
        self.rpc.delay = 2
        start = self.budget_clock()
        result = self.step()
        self.assertLessEqual(self.budget_clock() - start, 20)
        self.assertEqual(self.turn_calls(), [])
        self.assertEqual(result["status"], "no_eligible_event")
        self.assertIn("snapshot_budget_exhausted", result["unavailable"].values())

    def test_missing_goal_response_field_fails_closed(self):
        self.enable(); self.queue_event()
        self.rpc.handlers["thread/goal/get"] = lambda params: {}
        self.step()
        self.assertEqual(self.turn_calls(), [])
        self.assertEqual(self.rt.status()["events"], {"pending": 1})

    def test_empty_goal_object_fails_closed(self):
        self.enable(); self.queue_event()
        self.rpc.goals["worker-1"] = {}
        self.step()
        self.assertEqual(self.turn_calls(), [])
        self.assertEqual(self.rt.status()["events"], {"pending": 1})

    def test_invalid_queue_or_terminal_data_fails_closed(self):
        self.enable(); self.queue_event()
        for field in ("queues", "terminals"):
            for data in (None, ""):
                with self.subTest(field=field, data=data):
                    self.rpc.queues.clear(); self.rpc.terminals.clear()
                    getattr(self.rpc, field)["worker-1"] = {"data": data, "nextCursor": None}
                    self.step()
                    self.assertEqual(self.turn_calls(), [])

    def test_read_only_check_uses_fake_existing_host_and_never_turn_start(self):
        binary, socket = self.native_fixture_paths()
        with patch.object(host, "Rpc", return_value=self.rpc) as factory:
            code, result = self.invoke("--check", "--codex", str(binary), "--sock", str(socket))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "read_only_connected")
        self.assertFalse(result["deliveryVerified"])
        self.assertTrue(self.rpc.initialized)
        self.assertTrue(self.rpc.closed)
        self.assertEqual(self.turn_calls(), [])
        command = factory.call_args.args[0]
        self.assertEqual(command, [str(binary), "app-server", "proxy", "--sock", str(socket)])
        self.assertFalse(self.rt.status()["developmentEnabled"])

    def test_enabled_cli_without_verification_never_constructs_rpc(self):
        self.enable()
        binary, socket = self.native_fixture_paths()
        with self.assertRaisesRegex(ValueError, "verification"):
            self.invoke("--once", "--codex", str(binary), "--sock", str(socket))
        self.blocked_rpc.assert_not_called()

    def test_verification_bound_to_policy_and_exact_transport(self):
        self.enable()
        binary, socket = self.native_fixture_paths()
        base = {"verified": True, "exclusiveDispatcher": True, "policySha256": self.rt.policy_hash,
                "codexPath": str(binary), "controlSocket": str(socket), "evidence": self.ref}
        receipt = self.root / "verification.json"
        for changes in ({"policySha256": "wrong"}, {"codexPath": str(self.root / "other.exe")},
                        {"controlSocket": str(self.root / "other.sock")}, {"exclusiveDispatcher": False},
                        {"verified": False}):
            with self.subTest(changes=changes):
                receipt.write_text(json.dumps(base | changes), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.invoke("--once", "--codex", str(binary), "--sock", str(socket), "--verification", str(receipt))
        self.blocked_rpc.assert_not_called()


    def test_wall_clock_rollback_does_not_extend_snapshot_deadline(self):
        self.rpc.delay = 3
        self.rpc.wall_shift_per_call = -3600
        wall_start, budget_start = self.clock(), self.budget_clock()
        with self.assertRaises(TimeoutError):
            self.native.snapshot("worker-1", deadline=budget_start + 5)
        self.assertLess(self.clock(), wall_start)
        self.assertEqual(self.budget_clock() - budget_start, 5)
        self.assertEqual([c["timeout"] for c in self.rpc.calls], [5, 2])
        self.assertEqual(self.rpc.timeout, 10)
        self.assertEqual(self.turn_calls(), [])

    def test_wall_clock_rollback_does_not_extend_send_budget(self):
        self.rpc.delay = 2
        self.rpc.delays["turn/start"] = 10
        self.rpc.wall_shift_per_call = -3600
        wall_start, budget_start = self.clock(), self.budget_clock()
        with self.assertRaises(TimeoutError):
            self.native.send({"recipient": "worker-1", "eventId": "event",
                              "taskBindingId": "task-1", "event": self.event()})
        self.assertLess(self.clock(), wall_start)
        self.assertEqual(self.budget_clock() - budget_start, 15)
        self.assertEqual(self.turn_calls()[0]["timeout"], 5)
        self.assertEqual(self.rpc.timeout, 10)

    def test_wall_clock_rollback_does_not_extend_shared_step_budget(self):
        self.enable()
        for index in range(1, 5):
            if index > 1:
                self.rt.assign("router", {"threadId": f"worker-{index}", "title": "Worker",
                                         "taskBindingId": f"task-{index}"}, self.ref)
            self.queue_event(eid=f"event-{index}", task=f"task-{index}")
        self.queue_event(eid="router-event", task="", kind="new_request")
        self.rpc.delay = 2
        self.rpc.wall_shift_per_call = -3600
        wall_start, budget_start = self.clock(), self.budget_clock()
        result = self.step()
        self.assertLess(self.clock(), wall_start)
        self.assertEqual(self.budget_clock() - budget_start, 20)
        self.assertEqual(self.turn_calls(), [])
        self.assertIn("snapshot_budget_exhausted", result["unavailable"].values())
        self.assertEqual(self.rt.status()["events"], {"pending": 5})

    def test_observed_at_remains_wall_time_with_independent_budget_clock(self):
        self.rpc.delay = 0.25
        self.rpc.wall_shift_per_call = -100
        snapshot = self.native.snapshot("worker-1")
        self.assertEqual(snapshot["observedAt"], self.clock())
        self.assertNotEqual(snapshot["observedAt"], self.budget_clock())
        self.assertEqual(self.budget_clock(), 51.25)


if __name__ == "__main__":
    unittest.main()
