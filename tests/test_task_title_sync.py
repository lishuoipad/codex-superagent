import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/codex-superagent/scripts"))
from task_runtime import TaskRuntime
from task_native_delivery import NativeDelivery


class TitleSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="title-sync-", dir=os.environ.get("RELAY_TEST_TMP"))
        self.root = Path(self.tmp.name)
        proof = self.root / "authority.json"
        proof.write_text('{"humanInstruction":"isolated fixture only"}', encoding="utf-8")
        self.auth = {"path": str(proof), "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()}
        self.title = "【Project·Worker1】old task"
        self.config = {"schema": 1, "teamId": "fixture", "hostId": "fixture-host",
                       "router": {"threadId": "router", "title": "Router"}, "maxInUseWorkers": 4,
                       "initialDevelopmentEnabled": False, "authorization": self.auth,
                       "dbPath": str(self.root / "runtime.sqlite3"),
                       "registryPath": str(self.root / "registry"), "statusPath": str(self.root / "status.json"),
                       "allowedEventSources": ["fixture"],
                       "workers": [{"threadId": "w1", "title": self.title, "taskBindingId": "t1", "state": "waiting"}]}
        self.rt = TaskRuntime(self.config)
        self.rt.initialize()
        self.nd = NativeDelivery(self.rt)

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, event="e1", task="t1"):
        self.rt.set_gate(True, "router", self.auth)
        self.nd.prepare("operator", {"eventId": event, "source": "fixture", "kind": "followup",
                                     "taskBindingId": task, "summary": "full-task receipt fixture"},
                        "fixture", self.auth, 100)

    def accept(self, event="e1", task="t1"):
        self.prepare(event, task)
        attempt = self.nd.begin("operator", "session", event,
                                {"threadId": "w1", "hostId": "fixture-host", "observedAt": 100,
                                 "status": "idle", "paused": False, "requiresAttention": False}, 100)
        self.nd.finish("operator", event, attempt["attemptToken"], "accepted", {"turnId": event}, 101)

    def consume(self, event="e1", task="t1"):
        self.accept(event, task)
        return self.nd.receipt("operator", event, "w1", task, "consumer-" + event, self.auth, 102)

    def snap(self, title=None, **extra):
        return {"threadId": "w1", "hostId": "fixture-host", "source": "codex", "observedAt": 103,
                "title": self.title if title is None else title, **extra}

    def check(self, snapshot=None):
        return self.nd.title_sync([snapshot or self.snap()], 103)

    def reassign(self, title="【Project·Worker1】new task"):
        self.rt.complete("router", "w1", self.auth)
        return self.rt.assign("router", {"threadId": "w1", "title": title, "taskBindingId": "t2"}, self.auth)

    def test_prepared_and_accepted_do_not_trigger_rename(self):
        self.prepare()
        self.assertEqual(self.check()["items"][0]["state"], "awaiting_current_task_consumption")
        self.accept("e2")
        self.assertEqual(self.check()["renameRequired"], 0)
        self.assertEqual(self.check()["items"][0]["state"], "awaiting_current_task_consumption")

    def test_consumption_hint_requires_native_comparison(self):
        result = self.consume()
        self.assertEqual(result["titleSync"]["expectedTitle"], self.title)
        self.assertNotIn("nativeTool", result["titleSync"])
        result = self.check(self.snap(title="outdated"))
        self.assertEqual(result["renameRequired"], 1)
        self.assertEqual(result["items"][0]["arguments"],
                         {"source": "codex", "threadId": "w1", "title": self.title})

    def test_matching_title_is_noop_and_repeat_receipt_is_idempotent(self):
        self.consume()
        again = self.nd.receipt("operator", "e1", "w1", "t1", "consumer-e1", self.auth, 104)
        self.assertTrue(again["duplicate"])
        self.assertEqual(self.check()["unresolved"], 0)
        self.assertNotIn("arguments", self.check()["items"][0])

    def test_new_task_waits_for_its_own_consumption(self):
        self.consume()
        self.reassign()
        self.assertEqual(self.check()["items"][0]["generation"], 2)
        self.assertEqual(self.check()["items"][0]["state"], "awaiting_current_task_consumption")
        old = self.nd.receipt("operator", "e1", "w1", "t1", "consumer-e1", self.auth, 104)
        self.assertIsNone(old["titleSync"])

    def test_new_task_renames_after_consumption_and_readback(self):
        self.consume()
        self.reassign()
        self.consume("e2", "t2")
        before = self.check()
        self.assertEqual(before["items"][0]["expectedTitle"], "【Project·Worker1】new task")
        self.assertEqual(before["renameRequired"], 1)
        after = self.check(self.snap(title="【Project·Worker1】new task"))
        self.assertEqual(after["items"][0]["state"], "matched")

    def test_completed_task_and_late_receipt_do_not_rename(self):
        self.consume()
        self.rt.complete("router", "w1", self.auth)
        self.assertEqual(self.check()["items"], [])
        late = self.nd.receipt("operator", "e1", "w1", "t1", "consumer-e1", self.auth, 104)
        self.assertIsNone(late["titleSync"])

    def test_wrong_host_source_or_stale_snapshot_is_not_actionable(self):
        self.consume()
        for change in ({"hostId": "other"}, {"source": "chatgpt"}, {"observedAt": 80},
                       {"observedAt": 104}, {"observedAt": True}, {"observedAt": float("nan")}):
            with self.subTest(change=change):
                result = self.check(self.snap(title="outdated", **change))
                self.assertEqual(result["renameRequired"], 0)
                self.assertEqual(result["items"][0]["state"], "fresh_matching_native_snapshot_required")

    def test_missing_duplicate_and_oversized_snapshots(self):
        self.consume()
        self.assertEqual(self.nd.title_sync([], 103)["items"][0]["state"], "native_title_snapshot_required")
        for snapshots in ([self.snap(), self.snap()], [None], [self.snap()] * 65):
            with self.assertRaises(ValueError):
                self.nd.title_sync(snapshots, 103)

    def test_metadata_check_allows_active_thread_without_device_operation(self):
        self.consume()
        self.assertEqual(self.check(self.snap(title="outdated", status="active"))["renameRequired"], 1)

    def test_prefix_change_rejected_without_reassigning(self):
        self.consume()
        with self.assertRaisesRegex(ValueError, "prefix"):
            self.reassign("【Project·Worker2】wrong owner")
        state = self.rt.status()["workers"][0]
        self.assertEqual((state["generation"], state["taskBindingId"]), (1, "t1"))

    def test_invalid_new_titles_rejected(self):
        self.consume()
        self.rt.complete("router", "w1", self.auth)
        for title in ("", "x" * 161, " leading", "line\nbreak", "【Project·Worker1】", None):
            with self.subTest(title=title), self.assertRaises(ValueError):
                self.rt.assign("router", {"threadId": "w1", "title": title, "taskBindingId": "t2"}, self.auth)

    def test_same_task_substep_does_not_replace_title(self):
        self.consume()
        result = self.rt.assign("router", {"threadId": "w1", "title": "substep", "taskBindingId": "t1"}, self.auth)
        self.assertFalse(result["changed"])
        self.assertEqual(self.rt.status()["workers"][0]["title"], self.title)

    def test_pauses_and_status_export_unchanged_by_title_check(self):
        self.consume()
        self.rt.worker_state("router", "w1", "paused", self.auth)
        self.rt.set_gate(False, "router", self.auth)
        before = self.rt.export()
        raw = Path(self.config["statusPath"]).read_bytes()
        self.check(self.snap(title="outdated"))
        after = self.rt.status()
        self.assertFalse(after["developmentEnabled"])
        self.assertEqual(after["workers"], before["workers"])
        self.assertEqual(after["events"], before["events"])
        self.assertEqual(after["gateRevision"], before["gateRevision"])
        self.assertEqual(Path(self.config["statusPath"]).read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
