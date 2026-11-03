"""Task-bound durable routing on Agent Relay's existing SQLite/registry primitives.

This is routing state, not a second product acceptance ledger. It does not create
Codex sessions or run shell commands. Native message delivery belongs to a
separately verified host adapter. Initial development is always paused.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import time
import uuid
from supervision_store import SupervisionStore, StoreIntegrityError, RevisionConflict, _SCHEMA, _json, _path

KINDS = {"followup", "dependency_ready", "result", "background_completed",
         "new_request", "out_of_scope", "human_action"}
IN_USE = {"active", "waiting", "paused"}
_TERMINAL_EVENTS = {"delivered", "discarded"}
_TABLES = (
    """CREATE TABLE IF NOT EXISTS routing_teams
    (team_id TEXT PRIMARY KEY, policy_hash TEXT NOT NULL, enabled INTEGER NOT NULL,
     revision INTEGER NOT NULL, host_token TEXT, host_until REAL NOT NULL DEFAULT 0,
     gate_evidence TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS routing_workers
    (team_id TEXT NOT NULL, thread_id TEXT NOT NULL, task_id TEXT NOT NULL,
     title TEXT NOT NULL, state TEXT NOT NULL, generation INTEGER NOT NULL,
     authority_json TEXT NOT NULL, PRIMARY KEY(team_id,thread_id))""",
    """CREATE TABLE IF NOT EXISTS routing_subscriptions
    (team_id TEXT NOT NULL, source_key TEXT NOT NULL, task_id TEXT NOT NULL,
     PRIMARY KEY(team_id,source_key))""",
    """CREATE TABLE IF NOT EXISTS routing_events
    (team_id TEXT NOT NULL, event_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
     payload_json TEXT NOT NULL, recipient TEXT NOT NULL, task_id TEXT NOT NULL,
     generation INTEGER NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
     not_before REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
     lease_token TEXT, gate_revision INTEGER, outcome_json TEXT,
     PRIMARY KEY(team_id,event_id))""",
)

def _id(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(label + " must be a nonempty string <=256 chars")
    return value

def _assigned_title(value, previous=None):
    """Preserve a formatted Worker prefix when assigning a new complete task."""
    if (not isinstance(value, str) or not value.strip() or len(value) > 160
            or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("Worker title must be a single nonempty line <=160 chars")
    for opening, closing in (("【", "】"), ("[", "]")):
        if previous and previous.startswith(opening) and closing in previous:
            prefix = previous.split(closing, 1)[0] + closing
            if not value.startswith(prefix) or not value[len(prefix):].strip():
                raise ValueError("preserve the existing project/Worker prefix and name the new task")
            break
    return value

def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("finite timestamp required")
    return float(value)

def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()

def verify_ref(ref):
    if not isinstance(ref, dict):
        raise ValueError("evidence reference required")
    path = Path(ref["path"])
    if not path.is_absolute() or not path.is_file():
        raise ValueError("absolute existing evidence path required")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("evidence exceeds bounded read")
    if hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
        raise ValueError("evidence hash mismatch")
    return {"path": str(path), "sha256": ref["sha256"]}

class TaskRuntime:
    def __init__(self, config):
        self.config = json.loads(_json(config))
        if config.get("schema") != 1:
            raise ValueError("unsupported routing schema")
        self.team_id = _id(config["teamId"], "teamId")
        self.namespace = "task-routing:" + self.team_id
        self.router = _id(config["router"]["threadId"], "router")
        self.host_id = _id(config["hostId"], "hostId")
        limit = config["maxInUseWorkers"]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
            raise ValueError("maxInUseWorkers must be an explicit positive limit <=64")
        if config.get("initialDevelopmentEnabled") is not False:
            raise ValueError("new deployments must initialize paused")
        self.limit = limit
        self.sources = set(config["allowedEventSources"])
        if not self.sources or any(not isinstance(x, str) or not x for x in self.sources):
            raise ValueError("explicit trusted source names required")
        for key in ("dbPath", "registryPath", "statusPath"):
            if not Path(config[key]).is_absolute():
                raise ValueError(key + " must be absolute")
        self.base = SupervisionStore(config["dbPath"], config["registryPath"])
        self.policy_hash = _digest(config)

    def initialize(self):
        """Explicit one-time registration; missing/replaced state cannot reset it."""
        verify_ref(self.config["authorization"])
        workers = self.config.get("workers", [])
        if len(workers) > self.limit:
            raise ValueError("initial worker count exceeds limit")
        ids = [_id(w["threadId"], "worker") for w in workers]
        tasks = [_id(w["taskBindingId"], "task") for w in workers]
        if self.router in ids or len(set(ids)) != len(ids) or len(set(tasks)) != len(tasks):
            raise ValueError("duplicate role or task owner")
        Path(self.base.db_path).parent.mkdir(parents=True, exist_ok=True)
        reg = self.base._registry(create=True)
        try:
            reg.execute("BEGIN IMMEDIATE")
            old = reg.execute("SELECT * FROM bindings WHERE run_id=?", (self.namespace,)).fetchone()
            if old:
                if old["db_path"] != self.base.db_path or old["contract_hash"] != self.policy_hash or old["state"] != "ready":
                    raise StoreIntegrityError("routing registry conflict")
                if not Path(self.base.db_path).is_file():
                    raise StoreIntegrityError("registered routing store missing")
                reg.commit()
                with self._tx() as con:
                    if not con.execute("SELECT 1 FROM routing_teams WHERE team_id=?", (self.team_id,)).fetchone():
                        raise StoreIntegrityError("registered team missing")
                return self.status()
            # A path already registered to another Relay run is reusable only with its store identity.
            related = reg.execute("SELECT * FROM bindings WHERE db_path=?", (self.base.db_path,)).fetchall()
            existed = Path(self.base.db_path).exists()
            if related and (not existed or any(x["state"] != "ready" for x in related)):
                raise StoreIntegrityError("existing registry binding is incomplete")
            con = self.base._connect(self.base.db_path, create=True)
            try:
                con.execute("BEGIN IMMEDIATE")
                has_meta = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
                if has_meta:
                    meta = self.base._metadata(con)
                    store_id = meta["store_id"]
                    if any(x["store_id"] != store_id for x in related):
                        raise StoreIntegrityError("store identity changed")
                else:
                    if related or existed and con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                        raise StoreIntegrityError("unrecognized existing database")
                    for statement in _SCHEMA:
                        con.execute(statement)
                    store_id = str(uuid.uuid4())
                    con.executemany("INSERT INTO metadata VALUES (?,?)", [("schema_version", "1"), ("store_id", store_id)])
                for statement in _TABLES:
                    con.execute(statement)
                # A lost registry must not silently re-adopt an existing team.
                if con.execute("SELECT 1 FROM routing_teams WHERE team_id=?", (self.team_id,)).fetchone():
                    raise StoreIntegrityError("unregistered existing team; inspect, do not reset")
                con.execute("INSERT INTO routing_teams VALUES (?,?,0,1,NULL,0,?)",
                            (self.team_id, self.policy_hash, _json(self.config["authorization"])))
                for w in workers:
                    state = w.get("state", "waiting")
                    if state not in IN_USE:
                        raise ValueError("initial workers must have unfinished task bindings")
                    con.execute("INSERT INTO routing_workers VALUES (?,?,?,?,?,?,?)",
                                (self.team_id, w["threadId"], w["taskBindingId"],
                                 w["title"][:160], state, 1, _json(self.config["authorization"])))
                con.commit()
                reg.execute("INSERT INTO bindings VALUES (?,?,?,?, 'ready')",
                            (self.namespace, self.base.db_path, self.policy_hash, store_id))
                reg.commit()
            except BaseException:
                con.rollback()
                raise
            finally:
                con.close()
        except BaseException:
            reg.rollback()
            raise
        finally:
            reg.close()
        return self.status()

    @contextmanager
    def _tx(self):
        reg = self.base._registry()
        try:
            bound = reg.execute("SELECT * FROM bindings WHERE run_id=?", (self.namespace,)).fetchone()
            if not bound or bound["state"] != "ready" or bound["db_path"] != self.base.db_path or bound["contract_hash"] != self.policy_hash:
                raise StoreIntegrityError("routing authority not registered or changed")
            con = self.base._connect(self.base.db_path)
            try:
                con.execute("BEGIN IMMEDIATE")
                if self.base._metadata(con)["store_id"] != bound["store_id"]:
                    raise StoreIntegrityError("routing database identity changed")
                team = con.execute("SELECT * FROM routing_teams WHERE team_id=?", (self.team_id,)).fetchone()
                if not team or team["policy_hash"] != self.policy_hash:
                    raise StoreIntegrityError("routing team/policy missing")
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise
            finally:
                con.close()
        finally:
            reg.close()

    def _actor(self, actor):
        if actor != self.router:
            raise ValueError("only the bound Router may change task routing")

    def set_gate(self, enabled, actor, authority):
        self._actor(actor)
        evidence = verify_ref(authority)
        if not isinstance(enabled, bool):
            raise ValueError("boolean gate required")
        # This is an explicit trusted operator action, never accepted from ingested events.
        with self._tx() as con:
            con.execute("UPDATE routing_teams SET enabled=?,revision=revision+1,gate_evidence=? WHERE team_id=?",
                        (int(enabled), _json(evidence), self.team_id))
        return self.status()

    def assign(self, actor, worker, authority):
        self._actor(actor)
        evidence = verify_ref(authority)
        thread, task = _id(worker["threadId"], "worker"), _id(worker["taskBindingId"], "task")
        if thread == self.router:
            raise ValueError("Router cannot be a technical Worker")
        with self._tx() as con:
            old = con.execute("SELECT * FROM routing_workers WHERE team_id=? AND thread_id=?", (self.team_id, thread)).fetchone()
            owner = con.execute("SELECT * FROM routing_workers WHERE team_id=? AND task_id=? AND state!='completed'", (self.team_id, task)).fetchone()
            if owner and owner["thread_id"] != thread:
                raise ValueError("task already has a live owner")
            if old and old["state"] != "completed":
                if old["task_id"] == task:
                    return {"changed": False, "threadId": thread}
                raise ValueError("finish current task before reassigning Worker")
            count = con.execute("SELECT count(*) FROM routing_workers WHERE team_id=? AND state!='completed'", (self.team_id,)).fetchone()[0]
            if count >= self.limit:
                raise ValueError("in-use Worker limit reached")
            title = _assigned_title(worker.get("title"), old["title"] if old else None)
            generation = old["generation"] + 1 if old else 1
            con.execute("""INSERT INTO routing_workers VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(team_id,thread_id) DO UPDATE SET task_id=excluded.task_id,title=excluded.title,
                state=excluded.state,generation=excluded.generation,authority_json=excluded.authority_json""",
                        (self.team_id, thread, task, title, "waiting", generation, _json(evidence)))
            return {"changed": True, "threadId": thread, "generation": generation}

    def complete(self, actor, thread_id, authority):
        self._actor(actor)
        evidence = verify_ref(authority)
        with self._tx() as con:
            old = con.execute("SELECT * FROM routing_workers WHERE team_id=? AND thread_id=?", (self.team_id, thread_id)).fetchone()
            if not old:
                raise ValueError("unknown Worker")
            if con.execute("SELECT 1 FROM routing_events WHERE team_id=? AND recipient=? AND state IN ('inflight','unknown')",
                           (self.team_id, thread_id)).fetchone():
                raise ValueError("resolve in-flight or unknown delivery before releasing Worker")
            con.execute("UPDATE routing_workers SET state='completed',authority_json=? WHERE team_id=? AND thread_id=?",
                        (_json(evidence), self.team_id, thread_id))
        return self.status()

    def subscribe(self, actor, source_key, task_id):
        self._actor(actor)
        _id(source_key, "sourceKey")
        with self._tx() as con:
            if not con.execute("SELECT 1 FROM routing_workers WHERE team_id=? AND task_id=? AND state!='completed'", (self.team_id, task_id)).fetchone():
                raise ValueError("subscription needs a live task owner")
            con.execute("""INSERT INTO routing_subscriptions VALUES (?,?,?)
                ON CONFLICT(team_id,source_key) DO UPDATE SET task_id=excluded.task_id""", (self.team_id, source_key, task_id))

    def ingest(self, event, now=None):
        now = _number(time.time() if now is None else now)
        event = json.loads(_json(event))
        eid = _id(event["eventId"], "eventId")
        if event.get("source") not in self.sources or event.get("kind") not in KINDS:
            raise ValueError("unknown source or event kind; events cannot resume development")
        if len(str(event.get("summary", ""))) > 1600:
            raise ValueError("use a bounded summary and evidence references")
        if set(event) - {"eventId", "source", "sourceKey", "kind", "taskBindingId", "summary", "evidenceRef"}:
            raise ValueError("unexpected event fields")
        if "evidenceRef" in event:
            verify_ref(event["evidenceRef"])
        digest = _digest(event)
        with self._tx() as con:
            old = con.execute("SELECT * FROM routing_events WHERE team_id=? AND event_id=?", (self.team_id, eid)).fetchone()
            if old:
                if old["payload_hash"] != digest:
                    raise ValueError("event identity reused with different content")
                return {"duplicate": True, "state": old["state"], "recipient": old["recipient"]}
            task = event.get("taskBindingId")
            sub = con.execute("SELECT task_id FROM routing_subscriptions WHERE team_id=? AND source_key=?",
                              (self.team_id, event.get("sourceKey", ""))).fetchone()
            if sub:
                if task and task != sub["task_id"]:
                    raise ValueError("explicit task conflicts with subscription")
                task = sub["task_id"]
            if event["kind"] in {"new_request", "out_of_scope", "human_action"}:
                recipient, generation = self.router, 0
            else:
                w = con.execute("SELECT * FROM routing_workers WHERE team_id=? AND task_id=? AND state!='completed'", (self.team_id, task)).fetchone()
                if not w:
                    raise ValueError("known-task event has no live owner; do not guess or reopen")
                recipient, generation = w["thread_id"], w["generation"]
            con.execute("INSERT INTO routing_events VALUES (?,?,?,?,?,?,?,'pending',?,?,0,NULL,NULL,NULL)",
                        (self.team_id, eid, digest, _json(event), recipient, task or "", generation, now, now))
            return {"duplicate": False, "state": "pending", "recipient": recipient}

    def acquire_host(self, token, now, ttl=60):
        _id(token, "host token")
        now = _number(now)
        if not 5 <= ttl <= 120:
            raise ValueError("lease TTL must be bounded")
        with self._tx() as con:
            row = con.execute("SELECT * FROM routing_teams WHERE team_id=?", (self.team_id,)).fetchone()
            if row["host_token"] not in {None, token} and row["host_until"] > now:
                return False
            if row["host_token"] != token:
                # A prior sender might have crossed the API boundary before crashing.
                con.execute("UPDATE routing_events SET state='unknown',outcome_json=? WHERE team_id=? AND state='inflight'",
                            (_json({"reason": "previous_host_ended_without_outcome"}), self.team_id))
            con.execute("UPDATE routing_teams SET host_token=?,host_until=? WHERE team_id=?", (token, now + ttl, self.team_id))
            return True

    def _snapshot_ok(self, snapshot, recipient, now):
        return (isinstance(snapshot, dict) and snapshot.get("threadId") == recipient
                and snapshot.get("hostId") == self.host_id
                and snapshot.get("status") == "idle"
                and isinstance(snapshot.get("observedAt"), (int, float))
                and not isinstance(snapshot.get("observedAt"), bool)
                and 0 <= now - snapshot["observedAt"] <= 15
                and snapshot.get("paused") is False
                and snapshot.get("requiresAttention") is False)

    def _native_owned(self, con, event_id):
        """Optional provider extension; old routing stores have no such table."""
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='routing_native_deliveries'").fetchone():
            return False
        return bool(con.execute("SELECT 1 FROM routing_native_deliveries WHERE team_id=? AND event_id=?",
                                (self.team_id, event_id)).fetchone())

    def claim(self, token, snapshots, now):
        now = _number(now)
        with self._tx() as con:
            team = con.execute("SELECT * FROM routing_teams WHERE team_id=?", (self.team_id,)).fetchone()
            if not team["enabled"]:
                return {"allowed": False, "reason": "development_paused"}
            if team["host_token"] != token or team["host_until"] <= now:
                return {"allowed": False, "reason": "host_lease_missing"}
            rows = con.execute("SELECT * FROM routing_events WHERE team_id=? AND state='pending' AND not_before<=? ORDER BY created,event_id", (self.team_id, now)).fetchall()
            for row in rows:
                if self._native_owned(con, row["event_id"]):
                    continue
                w = con.execute("SELECT * FROM routing_workers WHERE team_id=? AND thread_id=?", (self.team_id, row["recipient"])).fetchone()
                if row["recipient"] != self.router and (not w or w["state"] == "completed" or w["generation"] != row["generation"] or w["task_id"] != row["task_id"]):
                    con.execute("UPDATE routing_events SET state='discarded',outcome_json=? WHERE team_id=? AND event_id=?",
                                (_json({"reason": "superseded_task_binding"}), self.team_id, row["event_id"]))
                    continue
                if w and w["state"] == "paused":
                    continue
                if con.execute("SELECT 1 FROM routing_events WHERE team_id=? AND recipient=? AND state IN ('inflight','unknown')", (self.team_id, row["recipient"])).fetchone():
                    continue
                if not self._snapshot_ok(snapshots.get(row["recipient"]), row["recipient"], now):
                    continue
                con.execute("UPDATE routing_events SET state='inflight',lease_token=?,gate_revision=?,attempts=attempts+1 WHERE team_id=? AND event_id=?",
                            (token, team["revision"], self.team_id, row["event_id"]))
                return {"allowed": True, "eventId": row["event_id"], "recipient": row["recipient"],
                        "taskBindingId": row["task_id"], "gateRevision": team["revision"],
                        "event": json.loads(row["payload_json"])}
            return {"allowed": False, "reason": "no_eligible_event"}

    def worker_state(self, actor, thread_id, state, authority):
        """Explicit per-task pause/resume. A team resume never clears this pause."""
        self._actor(actor)
        if state not in IN_USE:
            raise ValueError("unfinished Worker state required")
        evidence = verify_ref(authority)
        with self._tx() as con:
            row = con.execute("SELECT state FROM routing_workers WHERE team_id=? AND thread_id=?", (self.team_id, thread_id)).fetchone()
            if not row or row["state"] == "completed":
                raise ValueError("no unfinished Worker")
            con.execute("UPDATE routing_workers SET state=?,authority_json=? WHERE team_id=? AND thread_id=?",
                        (state, _json(evidence), self.team_id, thread_id))
            con.execute("UPDATE routing_teams SET revision=revision+1 WHERE team_id=?", (self.team_id,))
        return self.status()

    def _admission(self, con, token, event_id, snapshot, now):
        team = con.execute("SELECT * FROM routing_teams WHERE team_id=?", (self.team_id,)).fetchone()
        row = con.execute("SELECT * FROM routing_events WHERE team_id=? AND event_id=?", (self.team_id, event_id)).fetchone()
        if row and self._native_owned(con, event_id):
            return None
        w = None if not row else con.execute("SELECT * FROM routing_workers WHERE team_id=? AND thread_id=?", (self.team_id, row["recipient"])).fetchone()
        binding_ok = bool(row and (row["recipient"] == self.router or (w and w["state"] not in {"paused", "completed"}
                          and w["generation"] == row["generation"] and w["task_id"] == row["task_id"])))
        ok = bool(row and row["state"] == "inflight" and row["lease_token"] == token
                  and team["host_token"] == token and team["host_until"] > now
                  and team["enabled"] and team["revision"] == row["gate_revision"] and binding_ok
                  and self._snapshot_ok(snapshot, row["recipient"], now))
        if not ok and row and row["state"] == "inflight" and row["lease_token"] == token:
            con.execute("UPDATE routing_events SET state='pending',lease_token=NULL WHERE team_id=? AND event_id=?", (self.team_id, event_id))
        return row if ok else None

    def before_send(self, token, event_id, snapshot, now):
        """Diagnostic compatibility check; NOT a permit. Actual hosts must use dispatch."""
        with self._tx() as con:
            return self._admission(con, token, event_id, snapshot, _number(now)) is not None

    def dispatch(self, token, event_id, snapshot, sender, now=None):
        """Serialize final admission and bounded native API call with pause/assignment.

        claim() has already committed inflight. If the process dies during sender,
        rollback leaves inflight and a new host marks it unknown, never a replay.
        sender must enforce its own short timeout (<30s), avoid reentering this
        database, and return {status: delivered|not_sent|unknown, evidence: {...}}.
        delivered denotes native API acceptance, not task completion. A pause
        commits before a new send or after an already admitted call; it cannot
        revoke a turn the native host has already accepted.
        """
        with self._tx() as con:
            now = _number(time.time() if now is None else now)
            row = self._admission(con, token, event_id, snapshot, now)
            if row is None:
                return {"sent": False, "reason": "admission_changed"}
            try:
                result = sender({"eventId": row["event_id"], "recipient": row["recipient"],
                                 "taskBindingId": row["task_id"], "event": json.loads(row["payload_json"])})
                if not isinstance(result, dict) or result.get("status") not in {"delivered", "not_sent", "unknown"}:
                    raise ValueError("invalid sender result")
                evidence = result.get("evidence", {})
                if not isinstance(evidence, dict) or len(_json(evidence)) > 8192:
                    raise ValueError("bounded delivery evidence required")
            except Exception as exc:
                result = {"status": "unknown"}
                evidence = {"reason": "sender_failed_after_admission", "errorType": type(exc).__name__}
            status = result["status"]
            not_before = row["not_before"]
            state = status
            if status == "not_sent":
                state = "pending" if row["attempts"] < 3 else "unknown"
                not_before = now + min(60, 2 ** row["attempts"])
            con.execute("UPDATE routing_events SET state=?,not_before=?,outcome_json=? WHERE team_id=? AND event_id=?",
                        (state, not_before, _json(evidence), self.team_id, event_id))
            return {"sent": status == "delivered", "status": status, "eventId": event_id, "evidence": evidence}

    def outcome(self, token, event_id, status, evidence, now=None):
        if status not in {"delivered", "not_sent", "unknown"}:
            raise ValueError("invalid delivery outcome")
        now = _number(time.time() if now is None else now)
        with self._tx() as con:
            row = con.execute("SELECT * FROM routing_events WHERE team_id=? AND event_id=?", (self.team_id, event_id)).fetchone()
            if not row or row["state"] != "inflight" or row["lease_token"] != token:
                raise ValueError("outcome does not own current delivery")
            state = status
            not_before = row["not_before"]
            if status == "not_sent":
                state = "pending" if row["attempts"] < 3 else "unknown"
                not_before = now + min(60, 2 ** row["attempts"])
            con.execute("UPDATE routing_events SET state=?,not_before=?,outcome_json=? WHERE team_id=? AND event_id=?",
                        (state, not_before, _json(evidence), self.team_id, event_id))

    def reconcile(self, actor, event_id, delivered, authority):
        """Caller must inspect native history. A nonempty file alone is not proof."""
        self._actor(actor)
        if not isinstance(delivered, bool):
            raise ValueError("delivered must be a boolean")
        evidence = verify_ref(authority)
        with self._tx() as con:
            if self._native_owned(con, event_id):
                raise ValueError("use native delivery reconciliation for this event")
            row = con.execute("SELECT state FROM routing_events WHERE team_id=? AND event_id=?", (self.team_id, event_id)).fetchone()
            if not row or row["state"] != "unknown":
                raise ValueError("only an uncertain delivery can be reconciled")
            con.execute("UPDATE routing_events SET state=?,outcome_json=? WHERE team_id=? AND event_id=?",
                        ("delivered" if delivered else "pending", _json(evidence), self.team_id, event_id))

    def pending_targets(self):
        with self._tx() as con:
            return [r[0] for r in con.execute("SELECT DISTINCT recipient FROM routing_events WHERE team_id=? AND state='pending'", (self.team_id,))]

    def status(self):
        with self._tx() as con:
            team = con.execute("SELECT * FROM routing_teams WHERE team_id=?", (self.team_id,)).fetchone()
            workers = [dict(r) for r in con.execute("SELECT * FROM routing_workers WHERE team_id=? ORDER BY thread_id", (self.team_id,))]
            states = {r[0]: r[1] for r in con.execute("SELECT state,count(*) FROM routing_events WHERE team_id=? GROUP BY state", (self.team_id,))}
            return {"schema": 1, "teamId": self.team_id, "updatedAt": time.time(),
                    "developmentEnabled": bool(team["enabled"]), "gateRevision": team["revision"],
                    "maxInUseWorkers": self.limit,
                    "inUseWorkers": sum(w["state"] != "completed" for w in workers),
                    "router": self.config["router"], "workers": [
                        {"threadId": w["thread_id"], "title": w["title"], "taskBindingId": w["task_id"],
                         "state": "paused" if not team["enabled"] and w["state"] != "completed" else w["state"],
                         "workState": w["state"], "generation": w["generation"]} for w in workers],
                    "events": states, "policySha256": self.policy_hash,
                    "nativeHost": self.config.get("nativeHost", {"verified": False})}

    def export(self):
        data = self.status()
        path = Path(self.config["statusPath"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return data

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("command", choices=["init", "status", "export", "ingest", "subscribe", "assign", "complete", "pause", "resume", "worker-state", "reconcile"])
    p.add_argument("--input", type=Path)
    args = p.parse_args()
    if not args.config.is_absolute():
        p.error("--config must be absolute")
    rt = TaskRuntime(json.loads(args.config.read_text(encoding="utf-8-sig")))
    data = {}
    if args.input:
        if not args.input.is_absolute() or args.input.stat().st_size > 65536:
            p.error("--input must be an absolute bounded JSON file")
        data = json.loads(args.input.read_text(encoding="utf-8-sig"))
    if args.command == "init": result = rt.initialize()
    elif args.command == "status": result = rt.status()
    elif args.command == "export": result = rt.export()
    elif args.command == "ingest": result = rt.ingest(data)
    elif args.command == "subscribe": result = rt.subscribe(data["actor"], data["sourceKey"], data["taskBindingId"])
    elif args.command == "assign": result = rt.assign(data["actor"], data["worker"], data["authorization"])
    elif args.command == "complete": result = rt.complete(data["actor"], data["threadId"], data["authorization"])
    elif args.command == "worker-state": result = rt.worker_state(data["actor"], data["threadId"], data["state"], data["authorization"])
    elif args.command in {"pause", "resume"}: result = rt.set_gate(args.command == "resume", data["actor"], data["authorization"])
    else: result = rt.reconcile(data["actor"], data["eventId"], data["delivered"], data["authorization"])
    if args.command not in {"status", "export"}:
        rt.export()
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()
