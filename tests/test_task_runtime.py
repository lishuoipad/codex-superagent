import hashlib, json, pathlib, sys, tempfile, threading, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "skills/codex-superagent/scripts"))
from task_runtime import TaskRuntime, StoreIntegrityError

class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.proof = self.root / "authority.json"
        self.proof.write_text('{"humanInstruction":"test-only authorization"}', encoding="utf-8")
        self.auth = {"path": str(self.proof), "sha256": hashlib.sha256(self.proof.read_bytes()).hexdigest()}
        self.config = {"schema":1, "teamId":"test-team", "hostId":"test-host",
            "router":{"threadId":"router","title":"Router"}, "maxInUseWorkers":4,
            "initialDevelopmentEnabled":False, "authorization":self.auth,
            "dbPath":str(self.root/"state.sqlite3"),"registryPath":str(self.root/"registry"),
            "statusPath":str(self.root/"status.json"),
            "allowedEventSources":["fixture"],
            "workers":[{"threadId":"w1","title":"W1","taskBindingId":"t1","state":"waiting"}]}
        self.rt=TaskRuntime(self.config);self.rt.initialize()
    def tearDown(self): self.tmp.cleanup()
    def snap(self, who="w1", now=100, status="idle"):
        return {"threadId":who,"hostId":"test-host","observedAt":now,"status":status,"paused":False,"requiresAttention":False}
    def event(self, eid="e1", **kw):
        return {"eventId":eid,"source":"fixture","kind":"followup","taskBindingId":"t1","summary":"bounded test",**kw}
    def enabled(self):
        self.rt.set_gate(True,"router",self.auth)
        self.assertTrue(self.rt.acquire_host("host",100))
    def test_init_and_restart_stay_paused(self):
        self.rt.ingest(self.event(),100);self.rt.acquire_host("host",100)
        again=TaskRuntime(self.config);again.initialize()
        self.assertFalse(again.status()["developmentEnabled"])
        self.assertEqual(again.claim("host",{"w1":self.snap()},100)["reason"],"development_paused")
        self.assertEqual(again.status()["events"],{"pending":1})
    def test_four_in_use_includes_waiting_paused(self):
        for i in range(2,5):
            self.rt.assign("router",{"threadId":f"w{i}","taskBindingId":f"t{i}","title":f"W{i}"},self.auth)
        self.assertEqual(self.rt.status()["inUseWorkers"],4)
        with self.assertRaisesRegex(ValueError,"limit"):
            self.rt.assign("router",{"threadId":"w5","taskBindingId":"t5","title":"W5"},self.auth)
        self.rt.complete("router","w4",self.auth)
        self.rt.assign("router",{"threadId":"w5","taskBindingId":"t5","title":"W5"},self.auth)
        s=self.rt.status()
        self.assertEqual(s["inUseWorkers"],4)
        self.assertEqual(len(s["workers"]),5)
    def test_concurrent_final_slot_only_once(self):
        for i in (2,3):self.rt.assign("router",{"threadId":f"w{i}","taskBindingId":f"t{i}","title":"W"},self.auth)
        barrier=threading.Barrier(2);results=[]
        def take(i):
            rt=TaskRuntime(self.config);barrier.wait()
            try:rt.assign("router",{"threadId":f"w{i}","taskBindingId":f"t{i}","title":"W"},self.auth);results.append(True)
            except ValueError:results.append(False)
        a=threading.Thread(target=take,args=(4,));b=threading.Thread(target=take,args=(5,))
        a.start();b.start();a.join();b.join()
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(self.rt.status()["inUseWorkers"],4)
    def test_router_cannot_take_worker_slot(self):
        with self.assertRaises(ValueError):self.rt.assign("router",{"threadId":"router","taskBindingId":"bad","title":"bad"},self.auth)
    def test_worker_cannot_change_routes_or_resume(self):
        with self.assertRaises(ValueError):self.rt.set_gate(True,"w1",self.auth)
        with self.assertRaises(ValueError):self.rt.subscribe("w1","s","t1")
    def test_subscription_routes_without_router(self):
        self.rt.subscribe("router","related-source","t1")
        e=self.event();e.pop("taskBindingId");e["sourceKey"]="related-source"
        self.assertEqual(self.rt.ingest(e,100)["recipient"],"w1")
    def test_mismatched_subscription_not_guessed(self):
        self.rt.subscribe("router","related-source","t1")
        with self.assertRaises(ValueError):self.rt.ingest(self.event(sourceKey="related-source",taskBindingId="other"),100)
    def test_duplicate_and_mutated_duplicate(self):
        self.rt.ingest(self.event(),100)
        self.assertTrue(self.rt.ingest(self.event(),101)["duplicate"])
        with self.assertRaises(ValueError):self.rt.ingest(self.event(summary="different"),101)
        self.assertEqual(self.rt.status()["events"],{"pending":1})
    def test_busy_keeps_event_then_idle_claims(self):
        self.enabled();self.rt.ingest(self.event(),100)
        self.assertFalse(self.rt.claim("host",{"w1":self.snap(status="active")},100)["allowed"])
        self.assertTrue(self.rt.claim("host",{"w1":self.snap()},100)["allowed"])
    def test_new_and_out_of_scope_go_router(self):
        self.enabled()
        self.rt.ingest(self.event(kind="out_of_scope"),100)
        c=self.rt.claim("host",{"router":self.snap("router")},100)
        self.assertEqual(c["recipient"],"router")
    def test_pause_between_claim_and_send(self):
        self.enabled();self.rt.ingest(self.event(),100)
        c=self.rt.claim("host",{"w1":self.snap()},100);self.assertTrue(c["allowed"])
        self.rt.set_gate(False,"router",self.auth)
        self.assertFalse(self.rt.before_send("host","e1",self.snap(),100))
        self.assertEqual(self.rt.status()["events"],{"pending":1})
        self.rt.set_gate(True,"router",self.auth)
        self.assertTrue(self.rt.claim("host",{"w1":self.snap()},100)["allowed"])
    def test_stale_or_wrong_host_snapshot_denied(self):
        self.enabled();self.rt.ingest(self.event(),100)
        for s in [self.snap(now=80),self.snap(now=101),self.snap()|{"hostId":"other"},self.snap()|{"requiresAttention":True}]:
            self.assertFalse(self.rt.claim("host",{"w1":s},100)["allowed"])
    def test_crash_after_claim_does_not_replay(self):
        self.enabled();self.rt.ingest(self.event(),100)
        self.rt.claim("host",{"w1":self.snap()},100)
        restarted=TaskRuntime(self.config)
        self.assertFalse(restarted.acquire_host("new",105))
        self.assertTrue(restarted.acquire_host("new",161))
        self.assertFalse(restarted.claim("new",{"w1":self.snap(now=161)},161)["allowed"])
        self.assertEqual(restarted.status()["events"],{"unknown":1})
    def test_sent_is_not_replayed_after_restart(self):
        self.enabled();self.rt.ingest(self.event(),100);self.rt.claim("host",{"w1":self.snap()},100)
        self.rt.outcome("host","e1","delivered",{"turnId":"native-test"},100)
        restarted=TaskRuntime(self.config);restarted.acquire_host("new",161)
        self.assertFalse(restarted.claim("new",{"w1":self.snap(now=161)},161)["allowed"])
        self.assertTrue(restarted.ingest(self.event(),162)["duplicate"])
    def test_unknown_send_blocks_recipient(self):
        self.enabled();self.rt.ingest(self.event(),100);self.rt.claim("host",{"w1":self.snap()},100)
        self.rt.outcome("host","e1","unknown",{"timeout":True},100);self.rt.ingest(self.event("e2"),100)
        self.assertFalse(self.rt.claim("host",{"w1":self.snap()},100)["allowed"])
        with self.assertRaises(ValueError):self.rt.complete("router","w1",self.auth)
    def test_superseded_task_event_not_delivered_to_new_task(self):
        self.enabled();self.rt.ingest(self.event(),100)
        self.rt.complete("router","w1",self.auth)
        self.rt.assign("router",{"threadId":"w1","taskBindingId":"new","title":"new"},self.auth)
        self.assertFalse(self.rt.claim("host",{"w1":self.snap()},100)["allowed"])
        self.assertEqual(self.rt.status()["events"],{"discarded":1})
    def test_event_cannot_unpause_or_create_worker(self):
        for e in [self.event(kind="resume"),self.event(developmentEnabled=True),self.event(kind="assign")]:
            with self.assertRaises(ValueError):self.rt.ingest(e,100)
        self.assertFalse(self.rt.status()["developmentEnabled"])
    def test_changed_policy_or_missing_db_does_not_reset(self):
        with self.assertRaises(StoreIntegrityError):TaskRuntime(self.config|{"maxInUseWorkers":5}).initialize()
        pathlib.Path(self.config["dbPath"]).unlink()
        with self.assertRaises(StoreIntegrityError):TaskRuntime(self.config).initialize()
    def test_export_does_not_enable_or_dispatch(self):
        self.rt.ingest(self.event(),100);self.rt.export()
        d=json.loads(pathlib.Path(self.config["statusPath"]).read_text())
        self.assertFalse(d["developmentEnabled"]);self.assertEqual(d["workers"][0]["state"],"paused")

if __name__=="__main__":unittest.main()
