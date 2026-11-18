"""Sanitized failure diagnosis for the existing tool-host supervision workflow."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from stalled_turn import active_review, finite


def capacity_error(error):
    """Only trusted protocol error fields; never infer capacity from HTTP 429/503 alone."""
    if not isinstance(error, dict):
        return False
    info = error.get("codexErrorInfo")
    code = error.get("code", "")
    if isinstance(info, str) and info not in ("serverOverloaded", "other"):
        return False
    if code and code not in ("server_overloaded", "server_is_overloaded", "model_overloaded"):
        return False
    if info == "serverOverloaded" or code:
        return True
    # Older clients wrap overload in generic stream/HTTP errors. Be conservative.
    text = str(error.get("message", "")).lower()[:4096]
    if any(x in text for x in ("quota", "usage limit", "rate limit", "unauthorized",
                               "context window", "额度", "限额", "上下文")):
        return False
    return any(x in text for x in ("model is temporarily overloaded", "model is overloaded",
                                   "model is at capacity", "model is busy",
                                   "模型繁忙", "模型容量不足"))


def classify_error(error):
    """Classify trusted protocol error fields without returning their raw text."""
    if not isinstance(error, dict):
        return {"kind": "unknown", "phase": "unknown"}
    message = str(error.get("message") or "")[:4096].lower()
    phase = "remote_compaction" if any(value in message for value in (
        "remote compact task", "remote compaction", "remotecompactionv2")) else "unknown"
    info = error.get("codexErrorInfo", error.get("codex_error_info"))
    codes = list(info) if isinstance(info, dict) else [info]
    codes.append(error.get("code"))
    normalized = {str(value).lower().replace("_", "").replace("-", "") for value in codes if value is not None}
    status = None
    if isinstance(info, dict):
        connection = info.get("httpConnectionFailed", info.get("http_connection_failed"))
        if isinstance(connection, dict):
            status = connection.get("httpStatusCode", connection.get("http_status_code"))
    if type(status) is not int:
        status = None
    detail = ""
    try:
        parsed = json.loads(str(error.get("message") or ""))
        if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
            detail = parsed["detail"].strip().lower()
    except (ValueError, TypeError):
        pass
    if normalized & {"usagelimitexceeded", "quotaexceeded", "insufficientquota"}:
        kind = "usage_limit"
    elif status in (401, 403) or normalized & {"unauthorized", "authenticationerror", "invalidapikey"}:
        kind = "authentication"
    elif status in (400, 422) or normalized & {"badrequest", "invalidrequesterror"} or detail == "bad request":
        kind = "request_rejected"
    elif normalized & {"contextwindowexceeded", "contextlengthexceeded"} or "context length exceeded" in message:
        kind = "context_limit"
    elif capacity_error(error):
        kind = "capacity"
    elif status == 429 or normalized & {"ratelimitexceeded", "ratelimited"}:
        kind = "rate_limit"
    elif any(value in message for value in ("stream disconnected", "transport error", "error decoding response body",
                                            "error sending request", "connection reset", "request timed out", "dns error")):
        kind = "network"
    elif status is not None and 500 <= status <= 599:
        kind = "service_unavailable"
    else:
        kind = "unknown"
    return {"kind": kind, "phase": phase, "httpStatus": status}


def failure_review(sample):
    """A failed-turn review request, never dispatch permission."""
    if (sample.get("status") not in {"idle", "systemError"} or sample.get("willRetry") or
            sample.get("turnStatus") != "failed" or sample.get("paused") or
            any(flag in sample.get("activeFlags", []) for flag in ("waitingOnApproval", "waitingOnUserInput"))):
        return None
    kind = sample.get("failureKind")
    if kind is None:
        return None
    if kind == "capacity":
        state, reason = "capacity_failure", "capacity_needs_checked_recovery"
    elif kind == "context_limit":
        state, reason = "context_limit_failure", "context_needs_diagnosis"
    elif kind in {"network", "service_unavailable"} or sample.get("failurePhase") == "remote_compaction" and kind == "unknown":
        state = "compaction_failure" if sample.get("failurePhase") == "remote_compaction" else "transport_failure"
        reason = "failure_needs_checked_recovery"
    elif kind == "request_rejected":
        state, reason = "request_rejected", "request_needs_diagnosis"
    elif kind in {"usage_limit", "rate_limit", "authentication"}:
        state, reason = "service_needs_action", "service_limit_or_authentication"
    else:
        state, reason = "failure_unknown", "failure_needs_diagnosis"
    return {"state": state, "reviewReason": reason, "reviewVersion": sample.get("turnId")}


def select_poll(value, thread_id):
    polls = value.get("polls", [value]) if isinstance(value, dict) else []
    matches = [poll for poll in polls if isinstance(poll, dict) and
               poll.get("thread", {}).get("id") == thread_id]
    if len(matches) != 1:
        raise ValueError("One matching thread snapshot is required")
    return matches[0]


def log_metadata(path, thread_id, turn_id, since, limit):
    """Read the existing indexed native logs; expose only selected metadata."""
    if not path.is_absolute() or not path.is_file():
        raise ValueError("An existing absolute log database path is required")
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True, timeout=2)) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT id,ts,level,target,feedback_log_body FROM logs "
            "WHERE thread_id=? AND ts>=? AND (level IN ('WARN','ERROR') OR target LIKE '%compact%' OR "
            "(target='codex_http_client::client' AND feedback_log_body LIKE '%Request completed method=POST %' "
            "AND feedback_log_body LIKE '%/codex/responses status=4__ %')) "
            "AND feedback_log_body LIKE ? ORDER BY ts DESC,ts_nanos DESC LIMIT ?",
            (thread_id, since, "%"+turn_id+"%", limit)).fetchall()
    result = []
    for log_id, stamp, level, target, body in reversed(rows):
        text = str(body or "")
        request = re.search(r"request=([A-Za-z0-9_]+)", text)
        request_id = re.search(r"(?:request_id|requestId)=\"?([A-Za-z0-9_-]{8,96})", text)
        response = None
        if target == "codex_http_client::client":
            response = re.search(r"Request completed method=POST url=https://chatgpt\.com/backend-api/codex/responses status=(4\d\d)\b[^\r\n]*? headers=(\{.*)", text)
        http_status, ray, server_id = None, None, None
        if response:
            http_status = int(response[1])
            try:
                headers, _ = json.JSONDecoder().raw_decode(response[2])
            except ValueError:
                headers = {}
            if isinstance(headers, dict):
                ray_value, id_value = headers.get("cf-ray"), headers.get("x-request-id")
                if isinstance(ray_value, str) and re.fullmatch(r"[a-fA-F0-9]{16}-[A-Z]{3}", ray_value):
                    ray = ray_value
                if isinstance(id_value, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,96}", id_value):
                    server_id = id_value
        details = {"message": text.rsplit("}:", 1)[-1]}
        if http_status:
            details["codexErrorInfo"] = {"httpConnectionFailed": {"httpStatusCode": http_status}}
        transport = "websocket" if "websocket" in text.lower() else "sse" if re.search(r"\bsse\b", text, re.I) else "unknown"
        result.append({"logId": log_id, "atUtc": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                       "level": level, "target": target, "requestKind": request.group(1) if request else None,
                       "requestId": server_id or (request_id.group(1) if request_id else None), "cfRay": ray, "transport": transport,
                       "error": classify_error(details)})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--logs", type=Path)
    parser.add_argument("--limit", type=int, default=20, help="Diagnostic row budget, 1..1000")
    parser.add_argument("--rollout", type=Path, help="Exact local rollout path for an active-turn review")
    parser.add_argument("--review-after-seconds", type=float, help="Review interval chosen by the calling host")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    uuid.UUID(args.thread_id)
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be 1..1000")
    if not args.output.is_absolute() or not args.output.parent.is_dir():
        parser.error("--output requires an existing absolute task directory")
    from supervision import public_sample, read_json
    snapshot, snapshot_hash = read_json(args.snapshot)
    poll = select_poll(snapshot, args.thread_id)
    now = time.time()
    sample = public_sample(poll, snapshot.get("observedAt"), poll["thread"].get("hostId", "local"),
                           snapshot.get("turnReads", {}).get(args.thread_id))
    settings = None
    if args.rollout or args.review_after_seconds is not None:
        if not (args.rollout and args.logs and finite(args.review_after_seconds) and args.review_after_seconds > 0):
            parser.error("active review requires --rollout, --logs and positive --review-after-seconds")
        settings = {"rollout": args.rollout, "logs": args.logs, "reviewAfterSeconds": args.review_after_seconds}
    turn = poll.get("latestTurn") or {}
    report = {"schema": 1, "threadId": args.thread_id, "turnId": sample["turnId"],
              "source": {"path": str(args.snapshot), "sha256": snapshot_hash},
              "threadStatus": sample["status"], "turnStatus": sample["turnStatus"],
              "nativeRetryInProgress": True if sample["willRetry"] else None if sample["status"] == "active" else False,
              "failure": {"kind": sample["failureKind"], "phase": sample["failurePhase"]},
              "review": active_review(sample, settings, now) or failure_review(sample), "dispatchPermission": False,
              "origin": "undetermined", "logs": {"status": "not_requested", "rows": []}}
    if args.logs:
        since = turn.get("startedAt")
        if type(since) not in (int, float) or not turn.get("id"):
            raise ValueError("Turn identity and start time are required for log correlation")
        try:
            rows = log_metadata(args.logs, args.thread_id, turn["id"], since, args.limit)
            report["logs"] = {"status": "read", "rows": rows, "rowBudget": args.limit,
                              "budgetReached": len(rows) == args.limit}
        except (OSError, sqlite3.Error, ValueError) as error:
            report["logs"] = {"status": "unavailable", "reason": type(error).__name__, "rows": []}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"report": str(args.output), "failure": report["failure"],
                      "review": report["review"], "nativeRetryInProgress": report["nativeRetryInProgress"],
                      "logRows": len(report["logs"]["rows"]), "dispatchPermission": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
