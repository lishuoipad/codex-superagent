"""Offline integration of original-plan selection, durable routing and MCP audit.

These fixtures are not native delivery or game-acceptance evidence.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/codex-superagent/scripts"))
from project_continuation import digest, inspect_project
from task_native_delivery import NativeDelivery
from task_runtime import TaskRuntime


class ContinuationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="continuation-integration-", dir=os.environ.get("RELAY_TEST_TMP"))
        self.root = Path(self.tmp.name)
        self.auth = self.proof("authority", {"instruction": "offline fixture only"})
        self.owner_ref = self.proof("owner-readiness", {"taskId": "G4-02", "reviewedBy": "worker-1", "ready": True})
        self.config = {"schema": 1, "teamId": "integration-fixture", "hostId": "fixture-host",
                       "router": {"threadId": "router", "title": "Router"},
                       "maxInUseWorkers": 4, "initialDevelopmentEnabled": False,
                       "authorization": self.auth, "dbPath": str(self.root / "routing.sqlite3"),
                       "registryPath": str(self.root / "registry"), "statusPath": str(self.root / "status.json"),
                       "allowedEventSources": ["fixture"], "workers": [
                           {"threadId": "worker-1", "taskBindingId": "G4-01", "title": "Worker1", "state": "active"},
                           {"threadId": "worker-2", "taskBindingId": "G4-03", "title": "Worker2", "state": "waiting"}]}
        self.rt = TaskRuntime(self.config); self.rt.initialize()
        self.rt.set_gate(True, "router", self.auth)
        self.native = NativeDelivery(self.rt)
        self.team = {"technicalTaskOwner": "worker-1", "businessRecordWriter": "worker-1",
                     "routingOwner": "router", "development": {"enabled": True}}
        self.plan = {"workObjective": {"scope": ["G4"]}, "tasks": [
            self.task("G4-01"), self.task("G4-02", developmentDependsOn=["G4-01"]), self.task("G4-03")]}
        self.ledger = {"continuation": {"schema": 1, "readyNextTasks": []}}

    def tearDown(self):
        self.tmp.cleanup()

    def proof(self, name, data):
        path = self.root / (name + ".json")
        path.write_text(json.dumps(data), encoding="utf-8")
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    @staticmethod
    def task(key, **kw):
        return {"id": key, "phase": "G4", "title": key, "status": "in-progress",
                "developmentDependsOn": [], "acceptanceDependsOn": [],
                "progressState": {"implementation": "in_progress", "acceptance": "pending",
                                  "basis": "Fixture current owner review"}, **kw}

    def report(self):
        return inspect_project(self.plan, self.ledger, self.team, self.rt.status())

    def ready_next(self, bound=False):
        item = self.plan["tasks"][1]
        entry = {"taskId": item["id"], "mode": "development", "planUnitSha256": digest(item),
                 "reviewedBy": "worker-1", "evidenceRef": self.owner_ref}
        if bound: entry["taskBindingId"] = item["id"]
        self.ledger["continuation"]["readyNextTasks"] = [entry]

    def complete_first(self):
        item = self.plan["tasks"][0]
        result = self.proof("task-1-accepted", {"taskId": item["id"], "implementation": "integrated",
                                               "applicableAcceptance": "passed", "unresolved": []})
        item["progressState"] = {"implementation": "integrated_pending_acceptance", "acceptance": "passed",
                                 "basis": "Fixture owner recorded applicable acceptance", "evidenceRef": result}
        return result

    def bind_next(self):
        self.complete_first(); self.ready_next()
        self.rt.complete("router", "worker-1", self.auth)
        action = self.report()["nextAction"]
        self.assertEqual(action["kind"], "route_ready")
        self.assertEqual(action["eligibleExistingWorkerIds"], ["worker-1"])
        self.rt.assign("router", {"threadId": "worker-1", "title": "Worker1", "taskBindingId": "G4-02"}, self.auth)
        self.ready_next(bound=True)

    def prepare(self, eid="G4-02-start"):
        event = {"eventId": eid, "source": "fixture", "kind": "followup", "taskBindingId": "G4-02",
                 "summary": "Continue the owner-reviewed next fixture task"}
        return self.native.prepare("router", event, "Event " + eid + ": execute G4-02 fixture", self.auth, 100)

    def begin(self, token="native-cycle-1", now=100):
        snap = {"threadId": "worker-1", "hostId": "fixture-host", "status": "idle", "observedAt": now,
                "paused": False, "requiresAttention": False}
        return self.native.begin("router", token, "G4-02-start", snap, now)

    def test_owner_evidence_then_release_selects_next_task_without_releasing_waiting_peer(self):
        self.ready_next()
        self.assertFalse(self.report()["readyCandidates"])
        self.complete_first()
        self.assertEqual(self.report()["nextAction"]["kind"], "waiting_for_existing_capacity")
        self.rt.complete("router", "worker-1", self.auth)
        report = self.report()
        self.assertEqual(report["nextAction"]["kind"], "route_ready")
        self.assertEqual(report["nextAction"]["candidate"]["taskId"], "G4-02")
        self.assertEqual(report["nextAction"]["eligibleExistingWorkerIds"], ["worker-1"])
        self.assertEqual(report["inUseWorkers"], 1)
        self.assertEqual(report["unfinishedBindings"][0]["threadId"], "worker-2")
        self.assertEqual(report["unfinishedBindings"][0]["state"], "waiting")

    def test_next_task_native_delivery_runs_while_other_worker_waits(self):
        self.bind_next()
        action = self.report()["nextAction"]
        self.assertEqual(action["kind"], "continue_owner")
        self.assertEqual(action["candidate"]["ownerThreadId"], "worker-1")
        self.prepare(); begun = self.begin()
        self.assertTrue(begun["begun"])
        self.assertEqual(begun["generation"], 2)
        status = self.native.finish("router", "G4-02-start", begun["attemptToken"], "accepted",
                                    {"nativeFixtureResponse": "accepted-turn-2"}, 101)
        self.assertTrue(status["accepted"]); self.assertFalse(status["consumed"])
        receipt = self.proof("consumer-2", {"eventId": "G4-02-start", "threadId": "worker-1", "turnId": "turn-2"})
        self.native.receipt("router", "G4-02-start", "worker-1", "G4-02", "turn-2", receipt, 102)
        self.assertTrue(self.native.status("G4-02-start")["consumed"])
        self.assertEqual(self.rt.status()["inUseWorkers"], 2)
        self.assertFalse(self.report()["completion"]["complete"])

    def test_duplicate_and_unknown_do_not_resend_or_free_capacity(self):
        self.bind_next(); self.prepare(); b = self.begin()
        self.native.finish("router", "G4-02-start", b["attemptToken"], "unknown", {"toolTimeout": True}, 101)
        self.assertTrue(self.prepare()["duplicate"])
        self.assertEqual(self.begin(token="new-cycle", now=102)["reason"], "event_unknown")
        self.assertEqual(self.native.status("G4-02-start")["attempts"], 1)
        with self.assertRaisesRegex(ValueError, "unknown delivery"):
            self.rt.complete("router", "worker-1", self.auth)
        self.assertEqual(self.rt.status()["inUseWorkers"], 2)
        self.assertFalse(self.report()["completion"]["complete"])

    def test_pause_stops_selection_and_next_native_begin(self):
        self.bind_next(); self.prepare()
        self.rt.set_gate(False, "router", self.auth)
        self.assertEqual(self.report()["nextAction"]["kind"], "paused")
        self.assertEqual(self.begin()["reason"], "development_paused")
        self.assertEqual(self.native.status("G4-02-start")["state"], "native_pending")
        self.assertEqual(self.native.status("G4-02-start")["attempts"], 0)

    def test_restart_same_registry_preserves_intent_and_marks_crash_unknown(self):
        self.bind_next(); self.prepare(); self.begin()
        rt2 = TaskRuntime(self.config)
        restarted_status = rt2.initialize()
        self.assertTrue(restarted_status["developmentEnabled"])
        self.assertEqual(restarted_status["events"], {"inflight": 1})
        self.rt = rt2; self.native = NativeDelivery(rt2)
        self.assertTrue(self.prepare()["duplicate"])
        self.assertEqual(self.begin(token="restarted-cycle", now=221)["reason"], "event_unknown")
        self.assertEqual(self.native.status("G4-02-start")["attempts"], 1)
        self.assertEqual(self.report()["nextAction"]["candidate"]["ownerThreadId"], "worker-1")

    def test_message_acceptance_and_worker_idle_are_not_project_completion(self):
        self.bind_next(); self.prepare(); b = self.begin()
        self.native.finish("router", "G4-02-start", b["attemptToken"], "accepted", {"fixture": "tool accepted"}, 101)
        self.rt.complete("router", "worker-1", self.auth)
        self.rt.complete("router", "worker-2", self.auth)
        report = self.report()
        self.assertEqual(report["inUseWorkers"], 0)
        self.assertFalse(report["completion"]["complete"])
        self.assertIn("G4-02", report["completion"]["remainingTaskIds"])
        self.assertFalse(report["completion"]["ownerReviewVerified"])


if __name__ == "__main__":
    unittest.main()
