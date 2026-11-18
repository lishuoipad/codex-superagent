"""Read-only correlation for an active local turn after a command finishes."""
import json
import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def running_tools(read, thread_id, host_id, turn_id, marker):
    """The native turn inventory may omit background work outside that turn."""
    if not isinstance(read, dict):
        return None
    thread = read.get("thread") or {}
    turns = read.get("turns") or []
    if (thread.get("id") != thread_id or thread.get("hostId") != host_id or not turns
            or turns[0].get("id") != turn_id or turns[0].get("status") != "inProgress"):
        return None
    items = turns[0].get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return None
    if not marker or not any(item.get("id") == marker and item.get("type") == "commandExecution"
                                              and item.get("status") == "completed" for item in items):
        return None
    known_tools = {"commandExecution", "mcpToolCall", "dynamicToolCall", "collabAgentToolCall", "fileChange"}
    terminal = {"completed", "failed", "declined", "cancelled", "canceled", "interrupted"}
    return sum(1 for item in items if item.get("status") not in terminal and
               (item.get("type") in known_tools or item.get("status") is not None))


def completed_command(path, thread_id, turn_id, marker):
    """Only accept a stable, complete final native record. Never scan the transcript."""
    if not path.is_absolute():
        return None
    before = path.stat()
    with path.open("rb") as stream:
        start = max(0, before.st_size - 128 * 1024)
        stream.seek(start)
        data = stream.read(128 * 1024)
    if not data.endswith(b"\n"):
        return None
    lines = data.rstrip(b"\r\n").rsplit(b"\n", 1)
    if not lines[-1] or (start and len(lines) == 1):
        return None
    record = json.loads(lines[-1])
    payload = record.get("payload") or {}
    item = payload.get("item") or {}
    if not (record.get("type") == "event_msg" and payload.get("type") == "item_completed"
            and payload.get("thread_id") == thread_id and payload.get("turn_id") == turn_id
            and item.get("id") == marker and item.get("type") == "CommandExecution"
            and item.get("status") == "completed" and item.get("exit_code") == 0):
        return None
    start_ms, end_ms = payload.get("started_at_ms"), payload.get("completed_at_ms")
    if not (finite(start_ms) and finite(end_ms) and 0 < start_ms <= end_ms):
        return None
    fingerprint = (before.st_ino, before.st_size, before.st_mtime_ns)
    return start_ms / 1000, end_ms / 1000, fingerprint


def last_request_failure(path, thread_id, turn_id, since):
    if not path.is_absolute():
        return None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute(
            "SELECT id, ts, target, feedback_log_body FROM logs WHERE thread_id=? AND ts>=? "
            "AND target IN ('codex_http_client::client','codex_http_client::request','codex_core::responses_retry') "
            "AND feedback_log_body LIKE ? ORDER BY ts DESC, ts_nanos DESC, id DESC LIMIT 1",
            (thread_id, int(since), "%" + turn_id + "%")).fetchone()
    if not row:
        return None
    ident, timestamp, target, body = row
    identity = re.search(r'(?:turn\.id|turn_id)="?([a-fA-F0-9-]{36})', body or "")
    if (target == "codex_http_client::client" and identity and identity[1] == turn_id
            and "Request failed method=POST" in body and "error sending request" in body
            and "/codex/responses" in body):
        return ident, timestamp
    return None


def active_review(sample, settings, now):
    """Advisory only. Observe through instrumented hosts with fresh matched reads."""
    observed = sample.get("observedAt")
    marker = sample.get("completedCommandId")
    if (not isinstance(settings, dict) or sample.get("hostId") != "local" or sample.get("status") != "active"
            or sample.get("turnStatus") != "inProgress" or not marker or not sample.get("turnId")
            or not sample.get("cursor") or sample.get("activeFlags") or sample.get("paused")
            or sample.get("willRetry") or sample.get("readError") or type(sample.get("runningTools")) is not int or sample["runningTools"] != 0
            or not finite(observed) or not 0 <= now - observed <= 120):
        return None
    quiet = settings.get("reviewAfterSeconds")
    if not finite(quiet) or quiet <= 0:
        return None
    try:
        path = Path(settings["rollout"])
        tool = completed_command(path, sample["threadId"], sample["turnId"], marker)
        if not tool or observed < tool[1] + quiet:
            return None
        failure = last_request_failure(Path(settings["logs"]), sample["threadId"], sample["turnId"], tool[0])
        if not failure or observed < failure[1] + quiet:
            return None
        stat = path.stat()
        if (stat.st_ino, stat.st_size, stat.st_mtime_ns) != tool[2]:
            return None
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
        return None
    return {"state": "suspected_stall", "reviewReason": "completed_command_without_followup",
            "reviewVersion": sample["turnId"] + "/" + marker,
            "stallEvidence": {"commandId": marker, "completedAt": tool[1], "failedRequestLogId": failure[0],
                              "failedRequestAt": failure[1], "observedAt": observed,
                              "reviewAfterSeconds": quiet, "otherBackgroundWork": "unverified"}}
