import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/codex-superagent/scripts"))
from task_runtime import TaskRuntime
from task_native_delivery import NativeDelivery


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="relay-native-", dir=os.environ.get("RELAY_TEST_TMP"))
        self.root = Path(self.tmp.name)
        self.proof = self.root / "authority.json"
        self.proof.write_text('{"humanInstruction":"fixture only"}', encoding="utf-8")
        self.auth = {"path": str(self.proof), "sha256": hashlib.sha256(self.proof.read_bytes()).hexdigest()}
        self.config = {"schema": 1, "teamId": "fixture-team", "hostId": "fixture-host",
                       "router": {"threadId": "router", "title": "Router"},
                       "maxInUseWorkers": 4, "initialDevelopmentEnabled": False,
                       "authorization": self.auth, "dbPath": str(self.root / "runtime.sqlite3"),
                       "registryPath": str(self.root / "registry"), "statusPath": str(self.root / "status.json"),
                       "allowedEventSources": ["fixture"],
                       "workers": [{"threadId": "w1", "title": "Worker1", "taskBindingId": "t1", "state": "waiting"}]}
        self.rt = TaskRuntime(self.config)
        self.rt.initialize()
        self.nd = NativeDelivery(self.rt)

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, eid="e1", **kw):
        return {"eventId": eid, "source": "fixture", "kind": "followup", "taskBindingId": "t1", "summary": "fixture", **kw}

    def snap(self, who="w1", now=100, **kw):
        return {"threadId": who, "hostId": "fixture-host", "observedAt": now, "status": "idle",
                "paused": False, "requiresAttention": False, **kw}

    def prepare(self, eid="e1", **kw):
        return self.nd.prepare("operator", self.event(eid, **kw), "[" + eid + "] fixture message", self.auth, 100)

    def begin(self, eid="e1", token="execution-1", now=100, snapshot=None):
        return self.nd.begin("operator", token, eid, snapshot or self.snap(now=now), now)

    def enable(self):
        self.rt.set_gate(True, "router", self.auth)

    def accepted(self):
        self.enable(); self.prepare(); attempt = self.begin()
        return self.nd.finish("operator", "e1", attempt["attemptToken"], "accepted", {"turnId": "accepted-turn"}, 101)

    def test_prepare_is_durable_and_does_not_enable(self):
        r = self.prepare()
        self.assertEqual(r["state"], "native_pending")
        self.assertFalse(self.rt.status()["developmentEnabled"])
        self.assertFalse(self.begin()["begun"])
        self.assertEqual(NativeDelivery(TaskRuntime(self.config)).status("e1")["state"], "native_pending")

    def test_duplicate_identity_does_not_replace_prompt_or_reset(self):
        self.prepare(); self.assertTrue(self.prepare()["duplicate"])
        with self.assertRaisesRegex(ValueError, "different prompt"):
            self.nd.prepare("operator", self.event(), "other body", self.auth, 100)
        with self.assertRaisesRegex(ValueError, "different content"):
            self.prepare(summary="other event")
        self.assertEqual(self.nd.status()["states"], {"native_pending": 1})

    def test_unknown_sources_and_unauthorized_hash_fail(self):
        with self.assertRaises(ValueError): self.prepare(source="untrusted")
        bad = self.auth | {"sha256": "0" * 64}
        with self.assertRaises(ValueError): self.nd.prepare("operator", self.event(), "body", bad, 100)

    def test_fresh_native_snapshot_and_pause_required(self):
        self.enable(); self.prepare()
        snapshots = [self.snap(now=80), self.snap(now=101), self.snap(hostId="elsewhere"),
                     self.snap(status="active"), self.snap(paused=True), self.snap(requiresAttention=True)]
        for snap in snapshots:
            self.assertEqual(self.begin(snapshot=snap)["reason"], "fresh_idle_native_snapshot_required")
        self.rt.worker_state("router", "w1", "paused", self.auth)
        self.assertEqual(self.begin()["reason"], "worker_paused")
        self.rt.worker_state("router", "w1", "waiting", self.auth)
        self.rt.set_gate(False, "router", self.auth)
        self.assertEqual(self.begin()["reason"], "development_paused")

    def test_begin_is_not_a_send_and_has_explicit_warning(self):
        self.enable(); self.prepare(); b = self.begin()
        self.assertTrue(b["begun"])
        self.assertIn("no cross-tool atomic pause", b["warning"])
        self.assertEqual(self.nd.status("e1")["state"], "inflight")
        self.assertFalse(self.nd.status("e1")["accepted"])
        self.assertEqual(b["nativeTool"], "send_message_to_thread")

    def test_concurrent_begin_has_one_winner(self):
        self.enable(); self.prepare()
        barrier = threading.Barrier(2); results = []
        def attempt(token):
            adapter = NativeDelivery(TaskRuntime(self.config)); barrier.wait()
            results.append(adapter.begin("operator", token, "e1", self.snap(), 100)["begun"])
        threads = [threading.Thread(target=attempt, args=(f"exec-{i}",)) for i in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.nd.status("e1")["attempts"], 1)

    def test_rpc_provider_is_excluded_and_old_store_still_works(self):
        self.enable(); self.rt.acquire_host("rpc", 100)
        self.rt.ingest(self.event("ordinary"), 100)
        self.assertTrue(self.rt.claim("rpc", {"w1": self.snap()}, 100)["allowed"])
        self.rt.outcome("rpc", "ordinary", "delivered", {}, 100)
        self.prepare()
        self.assertFalse(self.rt.claim("rpc", {"w1": self.snap()}, 100)["allowed"])
        self.assertEqual(self.begin()["reason"], "other_dispatcher_lease_active")

    def test_rpc_dispatch_cannot_send_native_attempt(self):
        self.enable(); self.prepare(); self.begin()
        called = []
        r = self.rt.dispatch("execution-1", "e1", self.snap(), lambda msg: called.append(msg), 100)
        self.assertFalse(r["sent"]); self.assertEqual(called, [])
        self.assertEqual(self.nd.status("e1")["state"], "inflight")

    def test_provider_cannot_adopt_already_attempted_rpc_event(self):
        self.enable(); self.rt.ingest(self.event(), 100); self.rt.acquire_host("rpc", 100)
        self.rt.claim("rpc", {"w1": self.snap()}, 100)
        with self.assertRaisesRegex(ValueError, "another provider"):
            self.prepare()

    def test_acceptance_does_not_mean_consumed(self):
        r = self.accepted()
        self.assertTrue(r["accepted"]); self.assertFalse(r["consumed"])
        self.assertEqual(self.begin(now=102)["reason"], "event_delivered")
        self.assertTrue(self.prepare()["duplicate"])

    def test_receipt_checks_identity_and_is_stable(self):
        self.accepted()
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.nd.receipt("operator", "e1", "elsewhere", "t1", "consumer-turn", self.auth, 103)
        r = self.nd.receipt("operator", "e1", "w1", "t1", "consumer-turn", self.auth, 103)
        self.assertTrue(r["consumed"])
        self.assertTrue(self.nd.receipt("operator", "e1", "w1", "t1", "consumer-turn", self.auth, 104)["duplicate"])
        with self.assertRaisesRegex(ValueError, "already recorded"):
            self.nd.receipt("operator", "e1", "w1", "t1", "new-turn", self.auth, 105)

    def test_unknown_blocks_release_and_same_recipient_but_not_other_worker(self):
        self.enable(); self.prepare(); a = self.begin()
        self.nd.finish("operator", "e1", a["attemptToken"], "unknown", {"timeout": True}, 101)
        self.prepare("e2")
        self.assertEqual(self.begin("e2", now=102)["reason"], "recipient_has_unresolved_delivery")
        with self.assertRaisesRegex(ValueError, "unknown delivery"):
            self.rt.complete("router", "w1", self.auth)
        self.rt.assign("router", {"threadId": "w2", "title": "W2", "taskBindingId": "t2"}, self.auth)
        self.prepare("other", taskBindingId="t2")
        b = self.begin("other", now=102, snapshot=self.snap("w2", 102))
        self.assertTrue(b["begun"])

    def test_crash_and_expiry_never_infer_not_sent(self):
        self.enable(); self.prepare(); self.begin()
        restarted = NativeDelivery(TaskRuntime(self.config))
        early = restarted.begin("operator", "new-exec", "e1", self.snap(now=102), 102)
        self.assertFalse(early["begun"])
        late = restarted.begin("operator", "new-exec", "e1", self.snap(now=221), 221)
        self.assertEqual(late["reason"], "event_unknown")
        self.assertEqual(restarted.status("e1")["attempts"], 1)
        self.assertTrue(restarted.prepare("operator", self.event(), "[e1] fixture message", self.auth, 222)["duplicate"])

    def test_pause_after_begin_is_recorded_without_false_atomic_claim(self):
        self.enable(); self.prepare(); b = self.begin()
        self.rt.set_gate(False, "router", self.auth)
        # Caller observes the new pause before invoking MCP, and records not_sent.
        self.nd.finish("operator", "e1", b["attemptToken"], "not_sent", {"reason": "new human pause"}, 101)
        self.assertEqual(self.nd.status("e1")["state"], "native_pending")
        self.assertEqual(self.begin(now=104)["reason"], "development_paused")

    def test_lone_crashed_inflight_can_be_explicitly_reconciled_after_expiry(self):
        self.enable(); self.prepare(); self.begin()
        restarted = NativeDelivery(TaskRuntime(self.config))
        self.assertEqual(restarted.status("e1")["state"], "inflight")
        with self.assertRaisesRegex(ValueError, "lease still active"):
            restarted.reconcile("router", "e1", "accepted", self.auth, {"historyChecked": True}, 219)
        r = restarted.reconcile("router", "e1", "accepted", self.auth,
                                {"historyChecked": True, "turnId": "found-native-turn"}, 221)
        self.assertTrue(r["accepted"])
        self.assertFalse(r["consumed"])
        self.assertEqual(r["attempts"], 1)

    def test_expired_inflight_not_sent_keeps_pause_and_attempt_count(self):
        self.enable(); self.prepare(); self.begin()
        self.rt.set_gate(False, "router", self.auth)
        self.nd.reconcile("router", "e1", "not_sent", self.auth,
                          {"historyChecked": True, "noNativeCallEvidence": "fixture"}, 221)
        self.assertEqual(self.nd.status("e1")["state"], "native_pending")
        self.assertEqual(self.nd.status("e1")["attempts"], 1)
        self.assertEqual(self.begin(now=222)["reason"], "development_paused")

    def test_not_sent_retries_bounded_and_reconciliation_no_budget_reset(self):
        self.enable(); self.prepare()
        for when in (100, 103, 108):
            b = self.begin(now=when)
            self.assertTrue(b["begun"])
            self.nd.finish("operator", "e1", b["attemptToken"], "not_sent", {"explicitNotCalled": True}, when)
        self.assertEqual(self.nd.status("e1")["state"], "unknown")
        self.nd.reconcile("router", "e1", "not_sent", self.auth, {"nativeHistoryChecked": True}, 120)
        self.assertEqual(self.begin(now=121)["reason"], "native_attempt_limit")

    def test_reconcile_native_only_and_accepted_does_not_resume(self):
        self.enable(); self.prepare(); b = self.begin()
        self.nd.finish("operator", "e1", b["attemptToken"], "unknown", {}, 101)
        with self.assertRaisesRegex(ValueError, "native delivery reconciliation"):
            self.rt.reconcile("router", "e1", False, self.auth)
        self.rt.set_gate(False, "router", self.auth)
        self.nd.reconcile("router", "e1", "accepted", self.auth, {"turnId": "proven-turn"}, 102)
        self.assertFalse(self.rt.status()["developmentEnabled"])
        self.assertTrue(self.nd.status("e1")["accepted"])

    def test_finish_rejects_wrong_operator_token_or_double_outcome(self):
        self.enable(); self.prepare(); b = self.begin()
        with self.assertRaisesRegex(ValueError, "does not own"):
            self.nd.finish("other", "e1", b["attemptToken"], "accepted", {}, 101)
        with self.assertRaisesRegex(ValueError, "does not own"):
            self.nd.finish("operator", "e1", "invalid", "accepted", {}, 101)
        self.nd.finish("operator", "e1", b["attemptToken"], "accepted", {}, 101)
        with self.assertRaisesRegex(ValueError, "no longer inflight"):
            self.nd.finish("operator", "e1", b["attemptToken"], "accepted", {}, 102)

    def test_old_prepared_event_cannot_reopen_reassigned_task(self):
        self.enable(); self.prepare()
        self.rt.complete("router", "w1", self.auth)
        self.rt.assign("router", {"threadId": "w1", "title": "Next", "taskBindingId": "t2"}, self.auth)
        self.assertEqual(self.begin()["reason"], "superseded_task_binding")
        self.assertEqual(self.nd.status("e1")["state"], "discarded")

    def test_rpc_ignores_native_even_if_generic_state_accidentally_pending(self):
        self.enable(); self.prepare()
        with self.rt._tx() as con:
            con.execute("UPDATE routing_events SET state='pending' WHERE event_id='e1'")
        self.rt.acquire_host("rpc", 100)
        self.assertFalse(self.rt.claim("rpc", {"w1": self.snap()}, 100)["allowed"])

    def test_router_direct_event_is_not_a_worker_slot(self):
        self.enable(); self.prepare(kind="new_request")
        self.assertEqual(self.begin(snapshot=self.snap("router"))["recipient"], "router")
        self.assertEqual(self.rt.status()["inUseWorkers"], 1)

    def test_status_has_bounded_pending_list_and_no_prompt(self):
        self.assertFalse(self.nd.status()["installed"])
        self.prepare(); s = self.nd.status()
        self.assertEqual(len(s["pending"]), 1)
        self.assertNotIn("prompt", s["pending"][0])
        self.assertFalse(s["pendingTruncated"])


if __name__ == "__main__":
    unittest.main()
