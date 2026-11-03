"""Independent routing behavior review. Only owned fixture databases and fake snapshots.

Run with AGENT_RELAY_REVIEW_BUILD set to a registered task's build directory.
No native message/queue/host/model or game operation is performed.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "codex-superagent" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from task_runtime import TaskRuntime
from supervision_store import SupervisionStore, StoreIntegrityError


class TaskRuntimeIndependentReview(unittest.TestCase):
    def setUp(self):
        self.build = Path(os.environ.get("AGENT_RELAY_REVIEW_BUILD", tempfile.gettempdir())).resolve()
        self.build.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-review-", dir=self.build)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(self.build))
        authorization = self.root / "test-authority.txt"
        authorization.write_text("LOCAL TEST ONLY: architecture fixtures, never native delivery.", encoding="utf-8")
        self.ref = {"path": str(authorization), "sha256": hashlib.sha256(authorization.read_bytes()).hexdigest()}
        self.config = {
            "schema": 1, "teamId": "review-team", "hostId": "fake-host",
            "router": {"threadId": "router", "title": "test Router"},
            "maxInUseWorkers": 4, "initialDevelopmentEnabled": False,
            "allowedEventSources": ["fake-source"],
            "dbPath": str(self.root / "authority.sqlite3"),
            "registryPath": str(self.root / "registry"),
            "statusPath": str(self.root / "status.json"),
            "authorization": self.ref,
            "workers": [self.worker("worker-1", "task-1")],
        }

    def tearDown(self):
        # Validate the exact owned target before TemporaryDirectory recursively removes it.
        self.assertTrue(self.root.resolve().is_relative_to(self.build))
        self.temp.cleanup()

    @staticmethod
    def worker(thread, task, state="waiting"):
        return {"threadId": thread, "taskBindingId": task, "title": thread, "state": state}

    def start(self, workers=None):
        if workers is not None:
            self.config["workers"] = workers
        self.rt = TaskRuntime(self.config)
        self.rt.initialize()
        return self.rt

    def enabled(self):
        self.rt.set_gate(True, "router", self.ref)
        self.assertTrue(self.rt.acquire_host("host-A", 1000))

    @staticmethod
    def event(event_id="event-1", task="task-1", **overrides):
        event = {"eventId": event_id, "source": "fake-source", "kind": "followup",
                 "taskBindingId": task, "summary": "Test-only event"}
        event.update(overrides)
        return event

    @staticmethod
    def snapshot(thread="worker-1", now=1000, **overrides):
        data = {"threadId": thread, "hostId": "fake-host", "status": "idle",
                "observedAt": now, "paused": False, "requiresAttention": False}
        data.update(overrides)
        return data

    def claim_one(self, event_id="event-1", now=1000):
        self.rt.ingest(self.event(event_id), now)
        claim = self.rt.claim("host-A", {"worker-1": self.snapshot(now=now)}, now)
        self.assertTrue(claim["allowed"], claim)
        return claim

    def test_four_in_use_include_waiting_paused_and_active(self):
        self.start([self.worker("worker-1", "task-1", "active"),
                    self.worker("worker-2", "task-2", "waiting"),
                    self.worker("worker-3", "task-3", "paused"),
                    self.worker("worker-4", "task-4", "waiting")])
        self.assertEqual(self.rt.status()["inUseWorkers"], 4)
        with self.assertRaisesRegex(ValueError, "limit"):
            self.rt.assign("router", self.worker("worker-5", "task-5"), self.ref)
        with self.assertRaisesRegex(ValueError, "Router"):
            self.rt.assign("router", self.worker("router", "task-router"), self.ref)
        self.rt.complete("router", "worker-2", self.ref)
        self.rt.assign("router", self.worker("worker-5", "task-5"), self.ref)
        status = self.rt.status()
        self.assertEqual(status["inUseWorkers"], 4)
        self.assertEqual(len(status["workers"]), 5)
        self.assertEqual(next(w for w in status["workers"] if w["threadId"] == "worker-2")["state"], "completed")

    def test_concurrent_assignment_never_exceeds_four(self):
        self.start([self.worker(f"worker-{i}", f"task-{i}") for i in range(1, 4)])
        barrier = threading.Barrier(2)
        def assign(i):
            rt = TaskRuntime(self.config)
            barrier.wait(timeout=10)
            try:
                rt.assign("router", self.worker(f"worker-{i}", f"task-{i}"), self.ref)
                return "accepted"
            except ValueError as error:
                self.assertIn("limit", str(error))
                return "limit"
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(assign, [4, 5]))
        self.assertCountEqual(result, ["accepted", "limit"])
        self.assertEqual(self.rt.status()["inUseWorkers"], 4)

    def test_pause_and_pending_event_survive_reopen_and_initialize(self):
        self.start()
        self.enabled()
        self.rt.ingest(self.event(), 1000)
        self.rt.set_gate(False, "router", self.ref)
        before = self.rt.status()
        reopened = TaskRuntime(copy.deepcopy(self.config))
        after = reopened.initialize()
        self.assertFalse(after["developmentEnabled"])
        self.assertEqual(after["gateRevision"], before["gateRevision"])
        self.assertEqual(after["events"], {"pending": 1})
        self.assertEqual(reopened.claim("host-A", {"worker-1": self.snapshot()}, 1000)["reason"], "development_paused")

    def test_reinitialize_does_not_restore_initial_worker_or_drop_subscription(self):
        self.start()
        self.rt.complete("router", "worker-1", self.ref)
        self.rt.assign("router", self.worker("worker-2", "task-2"), self.ref)
        self.rt.subscribe("router", "completion:tool-1", "task-2")
        reopened = TaskRuntime(copy.deepcopy(self.config))
        reopened.initialize()
        routed = reopened.ingest(self.event(task=None, sourceKey="completion:tool-1", kind="background_completed"), 1000)
        self.assertEqual(routed["recipient"], "worker-2")
        self.assertEqual(reopened.status()["inUseWorkers"], 1)
        self.assertEqual(next(w for w in reopened.status()["workers"] if w["threadId"] == "worker-1")["state"], "completed")

    def test_known_subscription_bypasses_router_and_rejects_conflicting_task(self):
        self.start()
        self.rt.subscribe("router", "completion:tool-1", "task-1")
        routed = self.rt.ingest(self.event(task=None, sourceKey="completion:tool-1", kind="background_completed"), 1000)
        self.assertEqual(routed["recipient"], "worker-1")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.rt.ingest(self.event("event-2", "other-task", sourceKey="completion:tool-1"), 1000)
        routed = self.rt.ingest(self.event("new-task", None, kind="new_request"), 1000)
        self.assertEqual(routed["recipient"], "router")

    def test_busy_event_is_retained_then_claimed_when_idle(self):
        self.start()
        self.enabled()
        self.rt.ingest(self.event(), 1000)
        denied = self.rt.claim("host-A", {"worker-1": self.snapshot(status="running")}, 1000)
        self.assertFalse(denied["allowed"])
        self.assertEqual(self.rt.status()["events"], {"pending": 1})
        self.assertTrue(self.rt.claim("host-A", {"worker-1": self.snapshot(now=1001)}, 1001)["allowed"])

    def test_duplicate_does_not_enqueue_again_or_change_payload(self):
        self.start()
        self.enabled()
        self.claim_one()
        self.rt.outcome("host-A", "event-1", "delivered", {"receipt": "fake-only"}, 1000)
        self.assertTrue(self.rt.ingest(self.event(), 1001)["duplicate"])
        self.assertEqual(self.rt.status()["events"], {"delivered": 1})
        with self.assertRaisesRegex(ValueError, "different content"):
            self.rt.ingest(self.event(summary="Different message"), 1001)
        self.assertFalse(self.rt.claim("host-A", {"worker-1": self.snapshot(now=1001)}, 1001)["allowed"])

    def test_unknown_send_blocks_automatic_retry_and_later_same_worker_event(self):
        self.start()
        self.enabled()
        self.claim_one()
        self.rt.outcome("host-A", "event-1", "unknown", {"reason": "fake-timeout"}, 1000)
        self.rt.ingest(self.event("event-2"), 1001)
        self.assertTrue(self.rt.ingest(self.event(), 1001)["duplicate"])
        self.assertFalse(self.rt.claim("host-A", {"worker-1": self.snapshot(now=1001)}, 1001)["allowed"])
        self.assertEqual(self.rt.status()["events"], {"pending": 1, "unknown": 1})
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.rt.complete("router", "worker-1", self.ref)

    def test_host_crash_after_claim_is_uncertain_and_not_resent(self):
        self.start()
        self.enabled()
        self.claim_one()
        reopened = TaskRuntime(copy.deepcopy(self.config))
        self.assertTrue(reopened.acquire_host("host-B", 1061))
        self.assertEqual(reopened.status()["events"], {"unknown": 1})
        self.assertFalse(reopened.claim("host-B", {"worker-1": self.snapshot(now=1061)}, 1061)["allowed"])
        self.assertFalse(reopened.before_send("host-A", "event-1", self.snapshot(now=1061), 1061))

    def test_pause_before_send_returns_claim_to_pending(self):
        self.start()
        self.enabled()
        self.claim_one()
        self.rt.set_gate(False, "router", self.ref)
        self.assertFalse(self.rt.before_send("host-A", "event-1", self.snapshot(now=1001), 1001))
        self.assertEqual(self.rt.status()["events"], {"pending": 1})
        self.assertFalse(self.rt.status()["developmentEnabled"])

    def test_pause_and_resume_invalidates_old_gate_revision(self):
        self.start()
        self.enabled()
        self.claim_one()
        self.rt.set_gate(False, "router", self.ref)
        self.rt.set_gate(True, "router", self.ref)
        self.assertFalse(self.rt.before_send("host-A", "event-1", self.snapshot(now=1001), 1001))
        self.assertEqual(self.rt.status()["events"], {"pending": 1})

    def test_post_send_pause_preserves_receipt_and_blocks_next_event(self):
        self.start()
        self.enabled()
        self.claim_one()
        self.assertTrue(self.rt.before_send("host-A", "event-1", self.snapshot(), 1000))
        fake_receipt = {"receipt": "fake delivery already happened before pause"}
        self.rt.set_gate(False, "router", self.ref)
        self.rt.outcome("host-A", "event-1", "delivered", fake_receipt, 1001)
        self.rt.ingest(self.event("event-2"), 1001)
        self.assertEqual(self.rt.status()["events"], {"delivered": 1, "pending": 1})
        self.assertFalse(self.rt.claim("host-A", {"worker-1": self.snapshot(now=1001)}, 1001)["allowed"])

    def test_paused_worker_remains_individually_paused_when_team_is_enabled(self):
        self.start([self.worker("worker-1", "task-1", "paused")])
        self.enabled()
        self.rt.ingest(self.event(), 1000)
        # Idle is not a resume: a durable per-worker pause must win over a native idle snapshot.
        claim = self.rt.claim("host-A", {"worker-1": self.snapshot()}, 1000)
        self.assertFalse(claim["allowed"], "paused Worker was admitted without an explicit Worker resume")
        self.assertEqual(self.rt.status()["events"], {"pending": 1})

    def test_reconcile_requires_boolean_delivery_decision(self):
        self.start()
        self.enabled()
        self.claim_one()
        self.rt.outcome("host-A", "event-1", "unknown", {"reason": "fake-timeout"}, 1000)
        with self.assertRaises((ValueError, TypeError)):
            self.rt.reconcile("router", "event-1", "false", self.ref)
        self.assertEqual(self.rt.status()["events"], {"unknown": 1})

    def test_registering_routing_preserves_existing_supervision_records(self):
        base = SupervisionStore(self.config["dbPath"], self.config["registryPath"])
        contract = {"start": 900, "deadline": 2000, "owner": {"threadId": "old-owner"},
                    "targets": [], "policy": {}}
        base.create_run("existing-run", contract, 1000)
        base.put_record("existing-run", "keep", {"original": True}, 0)
        before = base.get_record("existing-run", "keep")
        self.start()
        self.assertEqual(base.get_record("existing-run", "keep"), before)
        self.assertFalse(self.rt.status()["developmentEnabled"])

    def test_lost_registry_is_not_silently_recreated_as_new_team(self):
        self.start()
        self.rt.ingest(self.event(), 1000)
        registry_file = Path(self.rt.base.registry_path)
        self.assertTrue(registry_file.resolve().is_relative_to(self.root))
        registry_file.rename(registry_file.with_suffix(".saved"))
        with self.assertRaises(StoreIntegrityError):
            TaskRuntime(self.config).initialize()
        with closing(sqlite3.connect(self.config["dbPath"])) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0], 1)


    def test_dispatch_never_calls_sender_when_pause_committed_first(self):
        self.start()
        self.enabled()
        self.claim_one()
        self.rt.set_gate(False, "router", self.ref)
        calls = []
        result = self.rt.dispatch("host-A", "event-1", self.snapshot(now=1001),
                                  lambda payload: calls.append(payload), now=1001)
        self.assertFalse(result["sent"])
        self.assertEqual(calls, [])
        self.assertEqual(self.rt.status()["events"], {"pending": 1})

    def test_dispatch_serializes_pause_after_send_has_started(self):
        self.start()
        self.enabled()
        self.claim_one()
        entered, release, pause_requested, pause_committed = [threading.Event() for _ in range(4)]
        timeline = []
        def sender(payload):
            timeline.append("sender_entered")
            entered.set()
            if not release.wait(5):
                raise TimeoutError("fake sender test release timed out")
            timeline.append("sender_accepted")
            return {"status": "delivered", "evidence": {"fakeReceipt": "one"}}
        def pause_worker():
            pause_requested.set()
            TaskRuntime(copy.deepcopy(self.config)).set_gate(False, "router", self.ref)
            timeline.append("pause_committed")
            pause_committed.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            delivery = pool.submit(self.rt.dispatch, "host-A", "event-1", self.snapshot(), sender, 1000)
            try:
                self.assertTrue(entered.wait(5))
                pausing = pool.submit(pause_worker)
                self.assertTrue(pause_requested.wait(5))
                self.assertFalse(pause_committed.wait(0.05), "pause committed while the already-admitted send boundary was still held")
            finally:
                release.set()
            result = delivery.result(timeout=5)
            pausing.result(timeout=5)
        self.assertTrue(result["sent"])
        self.assertLess(timeline.index("sender_accepted"), timeline.index("pause_committed"))
        self.assertFalse(self.rt.status()["developmentEnabled"])
        self.assertEqual(self.rt.status()["events"], {"delivered": 1})
        self.assertEqual(self.rt.status()["workers"][0]["workState"], "waiting", "queue/API acceptance must not complete a task")

    def test_dispatch_exception_is_unknown_and_never_automatically_retried(self):
        self.start()
        self.enabled()
        self.claim_one()
        calls = []
        def sender(payload):
            calls.append(payload)
            raise TimeoutError("fake post-boundary timeout")
        result = self.rt.dispatch("host-A", "event-1", self.snapshot(), sender, now=1000)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.rt.status()["events"], {"unknown": 1})
        again = self.rt.dispatch("host-A", "event-1", self.snapshot(now=1001), sender, now=1001)
        self.assertFalse(again["sent"])
        self.assertEqual(len(calls), 1)
        self.rt.ingest(self.event("event-2"), 1001)
        self.assertFalse(self.rt.claim("host-A", {"worker-1": self.snapshot(now=1001)}, 1001)["allowed"])

    def test_dispatch_abrupt_exit_rolls_back_to_inflight_then_new_host_marks_unknown(self):
        self.start()
        self.enabled()
        self.claim_one()
        def sender(payload):
            raise SystemExit("test-only simulated process exit")
        with self.assertRaises(SystemExit):
            self.rt.dispatch("host-A", "event-1", self.snapshot(), sender, now=1000)
        self.assertEqual(self.rt.status()["events"], {"inflight": 1})
        reopened = TaskRuntime(copy.deepcopy(self.config))
        self.assertTrue(reopened.acquire_host("host-B", 1061))
        self.assertEqual(reopened.status()["events"], {"unknown": 1})
        self.assertFalse(reopened.claim("host-B", {"worker-1": self.snapshot(now=1061)}, 1061)["allowed"])

    def test_worker_pause_survives_team_resume_and_requires_explicit_worker_resume(self):
        self.start()
        self.enabled()
        self.claim_one()
        self.rt.worker_state("router", "worker-1", "paused", self.ref)
        self.rt.set_gate(False, "router", self.ref)
        self.rt.set_gate(True, "router", self.ref)
        calls = []
        result = self.rt.dispatch("host-A", "event-1", self.snapshot(now=1001),
                                  lambda payload: calls.append(payload), now=1001)
        self.assertFalse(result["sent"])
        self.assertEqual(calls, [])
        reopened = TaskRuntime(copy.deepcopy(self.config))
        reopened.initialize()
        self.assertEqual(reopened.status()["workers"][0]["workState"], "paused")
        self.assertFalse(reopened.claim("host-A", {"worker-1": self.snapshot(now=1001)}, 1001)["allowed"])
        reopened.worker_state("router", "worker-1", "waiting", self.ref)
        self.assertTrue(reopened.claim("host-A", {"worker-1": self.snapshot(now=1002)}, 1002)["allowed"])

    def test_concurrent_duplicate_dispatch_calls_sender_once(self):
        self.start()
        self.enabled()
        self.claim_one()
        barrier = threading.Barrier(2)
        calls = []
        def sender(payload):
            calls.append(payload)
            return {"status": "delivered", "evidence": {"fakeReceipt": "exactly-one"}}
        def dispatch_worker():
            barrier.wait(timeout=5)
            return TaskRuntime(copy.deepcopy(self.config)).dispatch("host-A", "event-1", self.snapshot(), sender, now=1000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(dispatch_worker) for _ in range(2)]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(result["sent"] for result in results), 1)
        self.assertEqual(self.rt.status()["events"], {"delivered": 1})

    def test_dispatch_checks_snapshot_time_after_waiting_for_database_writer(self):
        self.start()
        self.enabled()
        self.claim_one()
        clock_read = threading.Event()
        fake_time = {"now": 1000}
        calls = []
        def clock():
            clock_read.set()
            return fake_time["now"]
        def sender(payload):
            calls.append(payload)
            return {"status": "delivered", "evidence": {"fakeReceipt": "must-not-send-stale"}}
        # A separate registered participant may temporarily own SQLite's write lock.
        # Advance only the fake clock; never wait a real 16 seconds.
        with closing(sqlite3.connect(self.config["dbPath"], isolation_level=None)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with patch("task_runtime.time.time", side_effect=clock):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self.rt.dispatch, "host-A", "event-1", self.snapshot(), sender)
                    clock_read.wait(0.05)
                    fake_time["now"] = 1016
                    blocker.rollback()
                    result = future.result(timeout=5)
        self.assertFalse(result["sent"], "admission reused the pre-lock timestamp after snapshot expired")
        self.assertEqual(calls, [])
        self.assertEqual(self.rt.status()["events"], {"pending": 1})


if __name__ == "__main__":
    unittest.main()
