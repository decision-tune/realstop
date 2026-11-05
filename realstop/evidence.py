"""Local shadow evidence capture. Text is data; this module makes no judgments."""
import copy
import hashlib
import json
from datetime import datetime

VERSION = "realstop.evidence.snapshot.v1"
PARSER_REVISION = "codex-custom-tools.v1"
# Provider-internal blocks are not missing user-visible evidence. Never retain
# their bodies, signatures or encrypted payloads in the snapshot.
INTERNAL_BLOCK_TYPES = frozenset(("thinking", "redacted_thinking", "encrypted_content",
                                  "reasoning", "advisor_tool_result"))


def _internal_block(block):
    return (block.get("type") in INTERNAL_BLOCK_TYPES or
            (block.get("type") == "server_tool_use" and block.get("name") == "advisor"))


def canonical_hash(value):
    """Hash JSON content, excluding only the snapshot's own top-level hash."""
    value = {k: v for k, v in value.items() if k != "content_sha256"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _time(value):
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _parts(record):
    """Recognize Claude content blocks and Codex response_item payloads."""
    item = record.get("payload", record)
    message = item.get("message", item)
    role = message.get("role", item.get("role", record.get("type", "unknown")))
    content = message.get("content", item.get("content", []))
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = content
    else:
        blocks = []
    if item.get("type") in ("function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output",
                            "web_search_call", "image_generation_call", "tool_search_call", "tool_search_output"):
        blocks = [item]
    texts, calls, results, modalities = [], [], [], []
    for block in blocks:
        if not isinstance(block, dict):
            modalities.append("unknown")
            continue
        kind = block.get("type", "unknown")
        if kind in ("text", "input_text", "output_text"):
            texts.append(str(block.get("text", "")))
        elif kind in ("tool_use", "function_call", "custom_tool_call"):
            calls.append({"call_id": block.get("id") if kind == "tool_use" else block.get("call_id"),
                          "name": block.get("name"),
                          "arguments": block.get("input", block.get("arguments")),
                          "launch_claim": bool(isinstance(block.get("input"), dict) and
                                               block["input"].get("run_in_background") is True)})
        elif kind in ("tool_result", "function_call_output", "custom_tool_call_output"):
            output = block.get("content", block.get("output"))
            if isinstance(output, list):
                output = [b for b in output if not (isinstance(b, dict)
                          and _internal_block(b))]
                modalities.extend(b.get("type", "unknown") for b in output
                                  if isinstance(b, dict) and b.get("type") not in
                                  ("text", "input_text", "output_text"))
            results.append({"call_id": block.get("tool_use_id", block.get("call_id")),
                            "output": output,
                            "is_error": block.get("is_error"),
                            "exit_code": block.get("exit_code")})
        elif not _internal_block(block):
            modalities.append(kind)
    return role, "\n".join(texts), calls, results, modalities


def build_snapshot(records, *, session_id, turn_id, cutoff_id, max_chars=100000,
                   approval_source_ids=(), verified_background=None):
    """Return a JSON snapshot of supplied records through the exact cutoff.

    Input order must be source order. Explicit parent chains select context;
    missing ancestry is reported, never repaired from adjacency. Verified
    background is caller-attested, not a claim this module checked a process.
    """
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 0:
        raise ValueError("max_chars must be a nonnegative integer")
    rows, ids = [], set()
    for index, raw in enumerate(records):
        record = copy.deepcopy(raw)
        rid = record.get("uuid", record.get("id", "record:%d" % index))
        if not isinstance(rid, str) or rid in ids:
            raise ValueError("record IDs must be unique strings")
        ids.add(rid)
        record["_id"] = rid
        record["_index"] = index
        rows.append(record)
        if rid == cutoff_id:
            break
    if not rows or rows[-1]["_id"] != cutoff_id:
        raise ValueError("cutoff_id not found")
    lookup = {r["_id"]: r for r in rows}
    cutoff_time = _time(rows[-1].get("timestamp"))
    warnings = []
    eligible = []
    for r in rows:
        timestamp = _time(r.get("timestamp"))
        if cutoff_time is not None and timestamp is not None and timestamp > cutoff_time:
            warnings.append({"source_id": r["_id"], "issue": "timestamp_after_cutoff"})
        elif r.get("session_id", r.get("sessionId", session_id)) != session_id:
            warnings.append({"source_id": r["_id"], "issue": "foreign_session"})
        else:
            eligible.append(r)
    allowed = {r["_id"] for r in eligible}
    chain, current, linkage = set(), cutoff_id, "complete"
    while current is not None:
        if current in chain or current not in allowed:
            linkage = "unknown"
            break
        chain.add(current)
        r = lookup[current]
        if "parentUuid" in r:
            current = r["parentUuid"]
        elif "parent_id" in r:
            current = r["parent_id"]
        else:
            linkage = "unknown"
            break
    # Unknown linkage permits a bounded source-order context, explicitly unverified.
    context_rows = eligible if linkage == "unknown" else [r for r in eligible if r["_id"] in chain]
    context_ids = {r["_id"] for r in context_rows}
    calls, results, context = [], [], []
    internal_omissions = []
    for r in context_rows:
        role, text, cs, _, modalities = _parts(r)
        item = r.get("payload", r)
        message = item.get("message", item)
        content = message.get("content", [])
        internal = [b.get("type") for b in content if isinstance(b, dict)
                    and _internal_block(b)] if isinstance(content, list) else []
        if item.get("type") in INTERNAL_BLOCK_TYPES:
            internal.append(item["type"])
        if internal:
            internal_omissions.append({"source_id": r["_id"], "block_types": internal,
                                       "classification": "provider_internal_non_evidence"})
        context.append({"source_id": r["_id"], "source_index": r["_index"],
                        "timestamp": r.get("timestamp"), "role": role, "text": text,
                        "modalities": modalities})
        for call in cs:
            calls.append(dict(call, source_id=r["_id"], source_index=r["_index"],
                              timestamp=r.get("timestamp"), turn_id=r.get("turn_id")))
    call_map = {}
    for call in calls:
        cid = call["call_id"]
        if cid is None or cid in call_map:
            warnings.append({"source_id": call["source_id"], "issue": "missing_or_duplicate_call_id"})
            call_map[cid] = None
        else:
            call_map[cid] = call
    for r in eligible:
        _, _, _, rs, _ = _parts(r)
        for result in rs:
            call = call_map.get(result["call_id"])
            issue = None
            if call is None:
                issue = "unmatched_receipt"
            elif r["_index"] < call["source_index"]:
                issue = "receipt_before_call"
            elif r.get("turn_id", call.get("turn_id") or turn_id) != (call.get("turn_id") or turn_id):
                issue = "foreign_turn_receipt"
            elif (_time(r.get("timestamp")) is not None and _time(call["timestamp"]) is not None
                  and _time(r["timestamp"]) < _time(call["timestamp"])):
                issue = "stale_receipt"
            if issue:
                warnings.append({"source_id": r["_id"], "issue": issue})
                continue
            results.append(dict(result, source_id=r["_id"], source_index=r["_index"],
                                timestamp=r.get("timestamp"), linkage="call_id",
                                sibling=r["_id"] not in context_ids))
    observed_calls = {r["call_id"] for r in results}
    missing = [c["call_id"] for c in calls if c["call_id"] not in observed_calls]
    approvals = [{"source_id": c["source_id"], "text": c["text"], "authorization": "unknown"}
                 for c in context if c["source_id"] in approval_source_ids]
    for rid in approval_source_ids:
        if rid not in context_ids:
            warnings.append({"source_id": rid, "issue": "approval_source_unavailable"})
    background = {"launch_claims": [c["source_id"] for c in calls if c["launch_claim"]],
                  "current_status": "unknown", "callback_status": "unknown", "attestation": None}
    if verified_background is not None:
        v = copy.deepcopy(verified_background)
        # A valid referenced pre-cutoff observation is necessary; no prose inference.
        if (v.get("source_id") in allowed and v.get("verified") is True
                and v.get("status") in ("running", "completed", "failed")
                and v.get("source_id") in {r["source_id"] for r in results}):
            background.update(current_status=v["status"], callback_status=v.get("callback_status", "unknown"),
                              attestation=v)
        else:
            warnings.append({"source_id": v.get("source_id"), "issue": "unverified_background"})
    used, truncated = 0, []
    def budget(value, path):
        nonlocal used
        if isinstance(value, str):
            room = max(0, max_chars - used)
            used += min(room, len(value))
            if len(value) > room:
                truncated.append({"path": path, "original_chars": len(value), "retained_chars": room})
            return value[:room]
        if isinstance(value, list):
            return [budget(v, path + "/" + str(i)) for i, v in enumerate(value)]
        if isinstance(value, dict):
            return {k: budget(v, path + "/" + k) for k, v in value.items()}
        return value
    # Budget content only, not source identifiers, statuses or provenance.
    for c in context:
        c["text"] = budget(c["text"], "context/" + c["source_id"] + "/text")
    for i, c in enumerate(calls):
        c["arguments"] = budget(c["arguments"], "tool_calls/%d/arguments" % i)
    for i, r in enumerate(results):
        r["output"] = budget(r["output"], "tool_results/%d/output" % i)
    for a in approvals:
        a["text"] = next(c["text"] for c in context if c["source_id"] == a["source_id"])
    snapshot = {"schema_version": VERSION, "mode": "local_shadow", "session_id": session_id,
                "turn_id": turn_id, "cutoff_id": cutoff_id, "cutoff_timestamp": rows[-1].get("timestamp"),
                "context": context, "tool_calls": calls, "tool_results": results,
                "approval_evidence": approvals, "background": background,
                "coverage": {"parser_revision": PARSER_REVISION, "linkage": linkage, "source_order_assumed": True,
                             "missing_receipt_call_ids": missing, "warnings": warnings,
                             "truncations": truncated, "max_chars": max_chars,
                             "nontext_present": any(c["modalities"] for c in context),
                             "internal_non_evidence_omitted": internal_omissions,
                             "complete": linkage == "complete" and not missing and not warnings
                             and not truncated and not any(c["modalities"] for c in context)}}
    sources = {c["source_id"]: c["text"] for c in context}
    for collection in (calls, results):
        for item in collection:
            rid = item["source_id"]
            observed = {k: v for k, v in item.items() if k not in
                        ("source_id", "source_index", "timestamp", "sibling", "linkage")}
            sources[rid] = sources.get(rid, "") + "\n" + json.dumps(
                observed, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    snapshot["sources"] = [{"source_id": rid, "text": text} for rid, text in sources.items()]
    snapshot["content_sha256"] = canonical_hash(snapshot)
    return snapshot
