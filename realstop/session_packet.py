"""Bounded transcript selection for one stop event; omitted history is explicit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from .evidence import _parts
from .native_verifier import MAX_INPUT, make_packet

MAX_FILE = 256 * 1024 * 1024
MAX_LINE = 16 * 1024 * 1024
META_BLOCKS = {'tool_addition', 'tool_reference'}


def _extract(record):
    """Keep provider tool metadata as non-owner content, not as modalities."""
    item = record.get('payload', record)
    message = item.get('message', item)
    content = message.get('content', []) if isinstance(message, dict) else []
    meta = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get('type') in META_BLOCKS:
                meta.append(block)
        if meta:
            record = json.loads(json.dumps(record))
            item = record.get('payload', record)
            message = item.get('message', item)
            message['content'] = [b for b in content if not (isinstance(b, dict) and b.get('type') in META_BLOCKS)]
        # Provider tool references may also be embedded in a tool-result body.
        record = json.loads(json.dumps(record))
        item = record.get('payload', record)
        message = item.get('message', item)
        for block in message.get('content', []):
            if isinstance(block, dict) and block.get('type') == 'tool_result' and isinstance(block.get('content'), list):
                meta.extend(b for b in block['content'] if isinstance(b, dict) and b.get('type') in META_BLOCKS)
                block['content'] = [b for b in block['content'] if not (isinstance(b, dict) and b.get('type') in META_BLOCKS)]
    role, text, calls, receipts, modalities = _parts(record)
    return role, text, calls, receipts, modalities, meta



class _ToolBranches:
    """Classify only proven Claude split-block/receipt edges; keep originals."""
    def __init__(self):
        self.nodes, self.positions = {}, {}
        self.ambiguous, self.used = set(), set()
        self.normalized = 0

    @staticmethod
    def _message(record):
        if any(record.get(k, False) is not False for k in ('isMeta', 'isSidechain', 'isCompactSummary')):
            return None
        message = record.get('message')
        if not isinstance(message, dict) or message.get('role') != record.get('type'):
            return None
        return message

    @classmethod
    def _result(cls, record):
        message = cls._message(record)
        if record.get('type') != 'user' or message is None:
            return None
        blocks = message.get('content')
        if not isinstance(blocks, list) or not blocks or any(
            not isinstance(b, dict) or b.get('type') != 'tool_result' for b in blocks):
            return None
        ids = tuple(b.get('tool_use_id') for b in blocks)
        if not all(isinstance(cid, str) and cid for cid in ids) or len(set(ids)) != len(ids):
            return None
        anchor = record.get('sourceToolAssistantUUID')
        if 'sourceToolAssistantUUID' in record and (not isinstance(anchor, str) or not anchor):
            return None
        return ('result', ids, anchor)

    def observe(self, record, rid):
        result = self._result(record)
        if result is not None:
            self.nodes[rid] = result
            return
        message = self._message(record)
        if record.get('type') != 'assistant' or message is None:
            return
        group = (message.get('id'), record.get('requestId'))
        index = record.get('apiBlockIndex')
        if not all(isinstance(v, str) and v and len(v) <= 256 for v in group) or type(index) is not int or index < 0:
            return
        position = (group, index)
        if position in self.positions:
            self.ambiguous.add(group)
            if group in self.used:
                raise ValueError('missing_or_branched_ancestry')
        self.positions[position] = rid
        blocks = message.get('content')
        if not isinstance(blocks, list) or not blocks or any(
            not isinstance(b, dict) or b.get('type') != 'tool_use' for b in blocks):
            return
        ids = tuple(b.get('id') for b in blocks)
        if all(isinstance(cid, str) and cid for cid in ids) and len(set(ids)) == len(ids):
            self.nodes[rid] = ('call', group, index, ids)

    @staticmethod
    def _matches(result, parent, call_ids):
        return (result is not None and result[0] == 'result' and
                set(result[1]).issubset(call_ids) and
                (result[2] is None or result[2] == parent))

    def permits(self, record, parent, first_child, call_sources, receipts):
        node, first = self.nodes.get(parent), self.nodes.get(first_child)
        result = self._result(record)
        if node is None or node[0] != 'call' or node[1] in self.ambiguous or first is None:
            return False
        call_ids = set(node[3])
        if (not self._matches(result, parent, call_ids) or
            any(call_sources.get(cid) != parent for cid in result[1]) or
            set(result[1]).intersection(receipts)):
            return False
        continuation = (first[0] == 'call' and first[1] == node[1] and first[2] == node[2] + 1)
        if not continuation and not self._matches(first, parent, call_ids):
            return False
        self.used.add(node[1])
        self.normalized += 1
        return True

def read_session_packet(payload):
    """Return (packet, path, full-file digest); reject unprovable or oversized scope."""
    if payload.get('hook_event_name') != 'Stop':
        raise ValueError('unsupported_event')
    session = payload.get('session_id')
    reply = payload.get('last_assistant_message')
    if not isinstance(session, str) or not session or not isinstance(reply, str) or not reply:
        raise ValueError('missing_boundary')
    path = Path(payload.get('transcript_path', '')).expanduser()
    digest = hashlib.sha256()
    owners, current, runtime, ids, children, diagnostics = [], [], [], set(), set(), set()
    calls, receipts, call_sources = set(), set(), {}
    tool_branches, first_children = _ToolBranches(), {}
    event_pending = None
    started, completed, newest_turn = set(), set(), None
    final_kind = None
    omitted = 0
    total = 0
    deadline = time.monotonic() + 5
    with path.open('rb') as stream:
        for index, line in enumerate(iter(lambda: stream.readline(MAX_LINE + 1), b'')):
            total += len(line)
            if time.monotonic() > deadline or total > MAX_FILE or len(line) > MAX_LINE:
                raise ValueError('transcript_limit')
            digest.update(line)
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError('malformed_record')
            if record.get('sessionId', record.get('session_id', session)) != session:
                raise ValueError('foreign_session')
            rid = str(record.get('uuid', record.get('id', 'line:%d' % index)))
            if record.get('type') == 'api-request-blob':
                # Provider diagnostics may contain repeated conversation/tool blocks.
                # They are not conversation nodes or authority, but may be parents.
                if rid in ids:
                    raise ValueError('duplicate_source')
                ids.add(rid)
                diagnostics.add(rid)
                continue
            if rid in ids:
                raise ValueError('duplicate_source')
            parent = record.get('parentUuid', record.get('parent_id'))
            conversational = record.get('type') in ('user', 'assistant', 'response_item')
            if conversational and parent is not None:
                if parent not in ids:
                    raise ValueError('missing_or_branched_ancestry')
                if (parent in children and parent not in diagnostics and not
                    tool_branches.permits(record, parent, first_children[parent], call_sources, receipts)):
                    raise ValueError('missing_or_branched_ancestry')
            if conversational and parent is not None and parent not in diagnostics:
                children.add(parent)
                first_children.setdefault(parent, rid)
            tool_branches.observe(record, rid)
            if not conversational:
                diagnostics.add(rid)
            ids.add(rid)
            if record.get('type') in ('compacted', 'compact_boundary') or record.get('subtype') == 'compact_boundary':
                raise ValueError('compacted_scope')
            item = record.get('payload', {})
            if record.get('type') == 'response_item' and isinstance(item, dict) and item.get('type') == 'agent_message':
                current.append({'source_id':rid+':agent-context', 'role':'tool',
                                'text':'Agent context, not owner authorization: '+json.dumps(item,ensure_ascii=False,sort_keys=True)})
                final_kind = 'tool'
                continue
            if record.get('type') == 'session_meta':
                if not isinstance(item, dict) or item.get('id') != session:
                    raise ValueError('foreign_session_metadata')
                continue
            if record.get('type') == 'turn_context':
                if not isinstance(item, dict):
                    raise ValueError('malformed_turn_context')
                tid = item.get('turn_id')
                if tid and tid != newest_turn:
                    omitted += len(current)
                    current, runtime, calls, receipts, call_sources = [], [], set(), set(), {}
                    final_kind = None
                    newest_turn = tid
                runtime = [{'source_id': rid + ':runtime', 'role': 'tool', 'text': json.dumps(item, ensure_ascii=False, sort_keys=True)}]
                continue
            if record.get('type') == 'event_msg':
                if not isinstance(item, dict):
                    raise ValueError('malformed_event')
                kind = item.get('type')
                tid = item.get('turn_id')
                if kind == 'task_started':
                    if tid and tid != newest_turn:
                        omitted += len(current)
                        current, runtime, calls, receipts, call_sources = [], [], set(), set(), {}
                        final_kind = None
                    newest_turn = tid
                    started.add(tid)
                elif kind == 'task_complete':
                    newest_turn = tid
                    completed.add(tid)
                elif kind == 'user_message':
                    text = item.get('message')
                    if not isinstance(text, str):
                        raise ValueError('nontext_owner')
                    source = {'source_id': rid + ':owner-event', 'role': 'user', 'text': text}
                    omitted += len(current)
                    current, calls, receipts, call_sources = [], set(), set(), {}
                    owners.append(source)
                    event_pending = text
                    final_kind = 'user'
                continue
            role, text, cs, rs, modalities, meta = _extract(record)
            if modalities:
                raise ValueError('nontext_evidence')
            synthetic = any(record.get(k) is True for k in ('isMeta', 'isSidechain', 'isCompactSummary'))
            owner = role == 'user' and not rs and not synthetic and bool(text)
            if owner:
                if event_pending == text:
                    event_pending = None
                    continue
                omitted += len(current)
                current, calls, receipts, call_sources = [], set(), set(), {}
                owners.append({'source_id': rid + ':text', 'role': 'user', 'text': text})
                final_kind = 'user'
            elif text:
                current.append({'source_id': rid + ':text', 'role': 'assistant' if role == 'assistant' and not synthetic else 'tool', 'text': text})
                final_kind = 'assistant' if role == 'assistant' and not synthetic else 'tool'
            if not owner:
                event_pending = None
            raw_item = record.get('payload', record)
            raw_message = raw_item.get('message', raw_item)
            raw_blocks = raw_message.get('content', []) if isinstance(raw_message, dict) else []
            if raw_item.get('type') in ('function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output'):
                raw_blocks = [raw_item]
            if not isinstance(raw_blocks, list):
                raw_blocks = []
            raw_calls = [b for b in raw_blocks if isinstance(b, dict) and b.get('type') in ('tool_use', 'function_call', 'custom_tool_call')]
            raw_results = [b for b in raw_blocks if isinstance(b, dict) and b.get('type') in ('tool_result', 'function_call_output', 'custom_tool_call_output')]
            for j, call in enumerate(cs):
                cid = call.get('call_id')
                if not isinstance(cid, str) or not cid or cid in calls:
                    raise ValueError('invalid_call_id')
                calls.add(cid)
                call_sources[cid] = rid
                current.append({'source_id': rid + ':call:%d' % j, 'role': 'assistant', 'text': json.dumps(raw_calls[j] if j < len(raw_calls) else call, ensure_ascii=False, sort_keys=True)})
                final_kind = 'call'
            for j, receipt in enumerate(rs):
                cid = receipt.get('call_id')
                if (cid not in calls or cid in receipts or
                    ('sourceToolAssistantUUID' in record and record['sourceToolAssistantUUID'] != call_sources.get(cid))):
                    raise ValueError('orphan_or_duplicate_receipt')
                receipts.add(cid)
                current.append({'source_id': rid + ':result:%d' % j, 'role': 'tool', 'text': json.dumps(raw_results[j] if j < len(raw_results) else receipt, ensure_ascii=False, sort_keys=True)})
                final_kind = 'receipt'
            for j, block in enumerate(meta):
                current.append({'source_id': rid + ':metadata:%d' % j, 'role': 'tool', 'text': json.dumps(block, ensure_ascii=False, sort_keys=True)})
    if not owners or calls != receipts:
        raise ValueError('missing_owner_or_unresolved_calls')
    turn = payload.get('turn_id')
    # Stop is itself a trusted boundary. task_complete may only be persisted
    # after the hook returns; require the current turn identity, not a future
    # completion event. The exact final reply is still checked below.
    if turn and newest_turn is not None and newest_turn != turn:
        raise ValueError('stale_or_incomplete_turn')
    last = next((s for s in reversed(current) if s['role'] == 'assistant'), None)
    if not last or final_kind != 'assistant' or last['text'] != reply:
        raise ValueError('stale_reply_boundary')
    packet = make_packet(owners + runtime + current, {'complete': True, 'truncated': False,
                         'history_omitted': True, 'omitted_source_count': omitted,
                         'turn_identity_observed': newest_turn is not None,
                         'scope': 'all observed owner instructions and current turn; earlier assistant/tool history omitted; semantic completeness not asserted',
                         'ancestry': 'validated original links where present; selection omits earlier non-owner sources',
                         'tool_receipt_branches_normalized': tool_branches.normalized})
    if len(json.dumps(packet, ensure_ascii=False)) > MAX_INPUT:
        raise ValueError('evidence_over_budget')
    return packet, path, digest.hexdigest()
