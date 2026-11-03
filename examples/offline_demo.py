"""Offline routing with fictional identities; no native calls or development."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/codex-superagent/scripts"))
from task_runtime import TaskRuntime

def main():
    with tempfile.TemporaryDirectory(prefix="superagent-demo-", dir=os.environ.get("RELAY_TEST_TMP")) as folder:
        root = Path(folder)
        proof = root / "fixture.json"
        proof.write_text('{"fixture":"offline only; not human authorization"}', encoding="utf-8")
        config = {
            "schema": 1, "teamId": "fictional-team", "hostId": "fictional-host",
            "router": {"threadId": "fictional-router", "title": "Router"},
            "maxInUseWorkers": 2, "initialDevelopmentEnabled": False,
            "dbPath": str(root / "routing.sqlite3"), "registryPath": str(root / "registry.sqlite3"),
            "statusPath": str(root / "status.json"),
            "authorization": {"path": str(proof), "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()},
            "allowedEventSources": ["offline-demo"],
            "workers": [{"threadId": "fictional-worker-a", "title": "Worker A",
                         "taskBindingId": "UI-01", "state": "waiting"}],
        }
        runtime = TaskRuntime(config)
        runtime.initialize()
        event = {"eventId": "demo-followup", "source": "offline-demo", "kind": "followup",
                 "taskBindingId": "UI-01", "summary": "Fictional dependency update"}
        known = runtime.ingest(event)
        new = runtime.ingest({"eventId": "demo-new-request", "source": "offline-demo",
                              "kind": "new_request", "summary": "Fictional new task"})
        duplicate = runtime.ingest(event)
        status = runtime.status()
        assert known["recipient"] == "fictional-worker-a"
        assert new["recipient"] == "fictional-router"
        assert duplicate["duplicate"] is True and status["developmentEnabled"] is False
        print(json.dumps({"knownTaskRecipient": known["recipient"],
                          "newRequestRecipient": new["recipient"],
                          "duplicateDetected": duplicate["duplicate"],
                          "developmentEnabled": status["developmentEnabled"],
                          "inUseWorkers": status["inUseWorkers"],
                          "maxInUseWorkers": status["maxInUseWorkers"],
                          "nativeMessagesSent": 0}, indent=2))

if __name__ == "__main__":
    main()
