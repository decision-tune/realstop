"""Experimental local task-evidence projection; no model or authority decisions.

Citations are offsets into the sanitized source index, never raw transcripts.
"""
import hashlib
import json
import math
import re
import sys
from collections import Counter

from .evidence import build_snapshot

VERSION = "realstop.evidence.view.v1"
# Conservative local scrubbing, not a guarantee that every secret is detected.
SECRET = re.compile(r'(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\b["\']?\s*[:=]\s*["\']?)([^\s"\',}]+)|\b(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,})\b|(https?://)([^/\s:@]+):([^/\s@]+)@')
WORD = re.compile(r"[a-z0-9_./-]{3,}", re.I)
CONTINUATION = re.compile(r"^\s*(continue|go on|keep going|proceed|do it)[.!\s]*$", re.I)


def _clean(text):
    def replace(match):
        if match.group(1):
            return match.group(1) + "[REDACTED]"
        if match.group(3):
            return "[REDACTED]"
        return match.group(4) + "[REDACTED]@"
    # Redact whole PEM blocks before inspecting individual lines.
    text = re.sub(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", text)
    text = re.sub(r"\bre_[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
    return re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]{8,}=*", r"\1[REDACTED]", SECRET.sub(replace, text))


def _text(value):
    if isinstance(value, str):
        return _clean(value)
    return _clean(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def build_source_index(records, *, session_id, turn_id, cutoff_id, source_prefix_sha256,
                       approval_source_ids=(), assignment_source_ids=(), final_source_id=None):
    """Normalize exact pre-cutoff records before capture's global character budget.

    The supplied hash identifies the actual source prefix; caller owns verifying it.
    No authority or semantic completeness is inferred from any source.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", source_prefix_sha256):
        raise ValueError("invalid source prefix hash")
    full = build_snapshot(records, session_id=session_id, turn_id=turn_id,
                          cutoff_id=cutoff_id, max_chars=sys.maxsize,
                          approval_source_ids=approval_source_ids)
    flagged = set()
    for index, record in enumerate(records):
        rid = record.get("uuid", record.get("id", "record:%d" % index))
        if any(record.get(k) is True for k in ("isMeta", "isSidechain", "isCompactSummary")):
            flagged.add(rid)
        if rid == cutoff_id:
            break
    units = []
    def add(kind, item, text, **extra):
        uid = "%s:%s:%d" % (item["source_id"], kind, len(units))
        units.append({"unit_id": uid, "source_id": item["source_id"],
                      "source_index": item["source_index"], "timestamp": item.get("timestamp"),
                      "kind": kind, "text": _text(text), **extra})
    for c in full["context"]:
        if c["text"]:
            add("message", c, c["text"], role="untrusted_meta" if c["source_id"] in flagged else c["role"], modalities=c["modalities"])
    for c in full["tool_calls"]:
        add("call", c, c["arguments"], role="assistant_tool_call", call_id=c["call_id"],
            name=c["name"], launch_claim=c["launch_claim"])
    for r in full["tool_results"]:
        add("result", r, r["output"], role="tool_result", call_id=r["call_id"],
            is_error=r["is_error"], exit_code=r["exit_code"], sibling=r["sibling"])
    return {"schema_version": "realstop.evidence.source_index.v1", "session_id": session_id,
            "turn_id": turn_id, "cutoff_id": cutoff_id, "final_source_id": final_source_id, "source_prefix_sha256": source_prefix_sha256,
            "units": units, "coverage": full["coverage"], "background": full["background"],
            "approval_source_ids": list(approval_source_ids), "assignment_source_ids": list(assignment_source_ids),
            "sanitization": "common credential patterns redacted; detection is not exhaustive"}


def _terms(value):
    return Counter(x.lower() for x in WORD.findall(value))


def _score(text, query):
    words = _terms(text)
    length = max(1, sum(words.values()))
    return sum(min(count, 2) * (freq * 2.2 / (freq + 1.2 * (0.25 + 0.75 * length / 80)))
               for word, count in query.items() if (freq := words.get(word, 0)))


def _quote(unit, session, prefix, start=0, end=None):
    text = unit["text"]
    end = len(text) if end is None else end
    return {"unit_id": unit["unit_id"], "session_id": session, "source_id": unit["source_id"],
            "source_index": unit["source_index"], "timestamp": unit["timestamp"],
            "source_prefix_sha256": prefix, "role": unit["role"], "start_char": start,
            "end_char": end, "quote": text[start:end]}


def _windows(unit, query):
    """Whole lines with one neighboring line; exact sanitized-source offsets."""
    text = unit["text"]
    lines = text.splitlines(keepends=True)
    if not lines:
        return [(0, len(text))]
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    chosen = [i for i, line in enumerate(lines) if _score(line, query) > 0 or
              re.search(r"\b(error|failed|success|exit|completed|pending)\b", line, re.I)]
    if not chosen:
        return [(0, len(text))]
    spans = []
    for i in chosen:
        a, b = offsets[max(0, i-1)], offsets[min(len(lines), i+2)]
        if spans and a <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))
    return spans


def project_evidence(snapshot, source_index, tokenizer, budget):
    """Select a deterministic evidence view; tokenizer takes text and returns an integer.

    The input budget applies to canonical JSON of `model_input`, including citations.
    The separate local omission ledger is not model input. No completeness is asserted.
    """
    if type(budget) is not int or not 0 < budget <= 6144 or not callable(tokenizer):
        raise ValueError("invalid token budget or tokenizer")
    for k in ("session_id", "turn_id", "cutoff_id"):
        if snapshot.get(k) != source_index.get(k):
            raise ValueError("snapshot/source identity mismatch")
    units = source_index["units"]
    prefix = source_index["source_prefix_sha256"]; session = source_index["session_id"]
    ordered = sorted(units, key=lambda u: (u["source_index"], u["unit_id"]))
    owners = [u for u in ordered if u["kind"] == "message" and u["role"] == "user"]
    assistants = [u for u in ordered if u["kind"] == "message" and u["role"] == "assistant"]
    reply = next((u for u in reversed(assistants) if u["source_id"] == snapshot["cutoff_id"]), None)
    if reply is None and source_index.get("final_source_id"):
        reply = next((u for u in assistants if u["source_id"] == source_index["final_source_id"]), None)
        if reply is not None and owners and reply["source_index"] < owners[-1]["source_index"]:
            reply = None
    assignment = owners[-1] if owners else None
    anchor_ids = {u["unit_id"] for u in (reply, assignment) if u}
    calls = {u["call_id"]: u for u in ordered if u["kind"] == "call" and u["call_id"]}
    result_counts = Counter(u["call_id"] for u in ordered if u["kind"] == "result")
    ambiguous_receipts = sum(u["kind"] == "result" and
                             (not calls.get(u["call_id"]) or result_counts[u["call_id"]] != 1)
                             for u in ordered)
    model = {"observed_reply": _quote(reply, session, prefix) if reply else None,
             "assignment_candidate": _quote(assignment, session, prefix) if assignment else None,
             "earlier_instructions": [], "tool_evidence": [],
             "coverage_notice": {"scope": "unknown", "authorization": "unknown",
                                 "omissions": "selected excerpt; omitted source evidence may be decisive",
                                 "linkage": source_index["coverage"].get("linkage", "unknown"),
                                 "missing_receipts": len(source_index["coverage"].get("missing_receipt_call_ids", [])),
                                 "source_warnings": len(source_index["coverage"].get("warnings", [])),
                                 "duplicate_receipt_calls": sum(n > 1 for n in result_counts.values()),
                                 "ambiguous_receipts": ambiguous_receipts,
                                 "receipt_uncertainty": "ambiguous receipts omitted; current outcome may be unknown" if ambiguous_receipts else "no ambiguous receipts detected",
                                 "nontext_present": bool(source_index["coverage"].get("nontext_present")),
                                 "snapshot_truncated": bool(snapshot.get("coverage", {}).get("truncations", []))}}
    def size():
        n = tokenizer(json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if type(n) is not int or n < 0:
            raise ValueError("tokenizer must return a nonnegative integer")
        return n
    coverage = {"source": source_index["coverage"], "background": source_index["background"],
                "scope": "unknown", "selector_completeness": "unknown",
                "continuation_scope_unresolved": bool(assignment and CONTINUATION.fullmatch(assignment["text"])),
                "reply_boundary": "exact_cutoff" if reply and reply["source_id"] == snapshot["cutoff_id"] else "caller_bound_final_source",
                "sanitization": source_index["sanitization"]}
    ledger = []
    if not reply or not assignment or size() > budget:
        return {"schema_version": VERSION, "status": "unsupported_over_budget" if reply and assignment else "unsupported_missing_anchor",
                "model_input": None, "token_count": None, "coverage": coverage,
                "omissions": [{"unit_id": u["unit_id"], "reason": "unsupported_anchor", "characters": len(u["text"])} for u in ordered]}
    query = _terms((assignment["text"] if assignment else "") + " " + (reply["text"] if reply else ""))
    # Tool names/arguments expose observed artifact identifiers without treating them as instructions.
    for u in ordered:
        if u["kind"] == "call" and (u["source_index"] > assignment["source_index"] or
                                    u["source_id"] in set(source_index.get("assignment_source_ids", ()))):
            query.update(_terms(u["text"]))
    earlier = [u for u in owners if u["unit_id"] not in anchor_ids]
    explicit = set(source_index.get("approval_source_ids", ()))
    previous = earlier[-1]["unit_id"] if earlier else None
    explicit.update(source_index.get("assignment_source_ids", ()))
    earlier.sort(key=lambda u: (-(u["source_id"] in explicit), -(u["unit_id"] == previous),
                                -_score(u["text"], query), -u["source_index"], u["unit_id"]))
    results = [u for u in ordered if u["kind"] == "result"]
    explicit_call_sources = set(source_index.get("assignment_source_ids", ()))
    def current_call(result):
        call = calls.get(result["call_id"])
        return bool(call and (call["source_index"] >= assignment["source_index"] or
                              call["source_id"] in explicit_call_sources))
    # Recency of the result alone cannot promote a stale call; tie relevance to call provenance.
    results.sort(key=lambda u: (-current_call(u), -_score(u["text"], query),
                                -u["source_index"], u["unit_id"]))
    used = set(anchor_ids); selected_spans = {}; remaining = budget - size(); instruction_cap = size() + remaining // 2
    for u in earlier:
        q = _quote(u, session, prefix); model["earlier_instructions"].append(q)
        if size() > instruction_cap:
            model["earlier_instructions"].pop()
        else:
            used.add(u["unit_id"])
    for u in results:
        call = calls.get(u["call_id"])
        if not call or result_counts[u["call_id"]] != 1:
            continue
        for start, end in _windows(u, query):
            item = {"call": _quote(call, session, prefix), "call_id": u["call_id"], "name": call["name"],
                    "result": _quote(u, session, prefix, start, end), "is_error": u["is_error"], "exit_code": u["exit_code"],
                    "status_conflict": (u["is_error"] is False and isinstance(u["exit_code"], int) and u["exit_code"] != 0) or (u["is_error"] is True and u["exit_code"] == 0),
                    "launch_claim": call["launch_claim"], "sibling_receipt": u["sibling"]}
            model["tool_evidence"].append(item)
            if size() > budget:
                model["tool_evidence"].pop()
            else:
                used.update((u["unit_id"], call["unit_id"]))
                selected_spans.setdefault(u["unit_id"], []).append((start, end))
    for u in ordered:
        if u["unit_id"] in selected_spans:
            spans = sorted(selected_spans[u["unit_id"]]); cursor = 0
            for start, end in spans:
                if start > cursor:
                    ledger.append({"unit_id": u["unit_id"], "start_char": cursor, "end_char": start,
                                   "characters": start-cursor, "reason": "unselected_window"})
                cursor = max(cursor, end)
            if cursor < len(u["text"]):
                ledger.append({"unit_id": u["unit_id"], "start_char": cursor, "end_char": len(u["text"]),
                               "characters": len(u["text"])-cursor, "reason": "unselected_window"})
        if u["unit_id"] not in used:
            reason = "ambiguous_or_missing_call_receipt" if u["kind"] == "result" and (not calls.get(u["call_id"]) or result_counts[u["call_id"]] != 1) else "not_selected_or_budget"
            ledger.append({"unit_id": u["unit_id"], "characters": len(u["text"]), "reason": reason})
    return {"schema_version": VERSION, "status": "candidate", "model_input": model,
            "token_count": size(), "coverage": coverage, "omissions": ledger}
