"""Opt-in, evidence-confirmed Stop hook shared by Claude Code and Codex.

No claim of semantic completeness is inferred from a file being readable. Large,
branched, malformed or unpaired traces abstain. This module never executes the
verifier's proposed action; it returns bounded feedback to the parent agent.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

from .evidence import _parts
from .native_verifier import make_packet, packet_hash, verify_packet, MAX_INPUT

MAX_FILE = 2_000_000
STOP = re.compile(r"^\s*(?:stop|pause|cancel|quit|abort)(?:\s+(?:now|here|this|the task|working))?[.!\s]*$", re.I)


def packet_from_records(records, payload):
    """Preserve visible text and tool receipts without promoting tool text to user authority."""
    if not isinstance(records, list) or not records:
        raise ValueError('missing_transcript')
    sid = payload.get('session_id')
    sources, calls, receipts, ids = [], set(), set(), set()
    parent_ids = set()
    final_kind = None
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError('malformed_record')
        rsid = record.get('sessionId', record.get('session_id', sid))
        if rsid != sid:
            raise ValueError('foreign_session')
        if record.get('type') in ('compacted', 'compact_boundary') or record.get('subtype') == 'compact_boundary':
            raise ValueError('compacted_scope_requires_review')
        if any(record.get(k) is True for k in ('isMeta', 'isSidechain', 'isCompactSummary')):
            raise ValueError('synthetic_or_sidechain_scope')
        rid = str(record.get('uuid', record.get('id', 'record:%d' % i)))
        if rid in ids:
            raise ValueError('duplicate_source')
        ids.add(rid)
        parent = record.get('parentUuid', record.get('parent_id'))
        if parent is not None:
            if parent not in ids or parent in parent_ids:
                raise ValueError('missing_or_branched_ancestry')
            parent_ids.add(parent)
        item = record.get('payload', {})
        if record.get('type') == 'session_meta':
            if not isinstance(item, dict) or item.get('id') != sid:
                raise ValueError('foreign_session_metadata')
            continue
        if record.get('type') == 'turn_context':
            # Keep runtime restrictions visible, never as owner authorization.
            if not isinstance(item, dict):
                raise ValueError('malformed_turn_context')
            sources.append({'source_id':rid+':runtime','role':'tool',
                            'text':'Runtime context (not owner authorization): '+json.dumps(item, ensure_ascii=False, sort_keys=True)})
            if payload.get('turn_id') and item.get('turn_id') and item['turn_id'] != payload['turn_id']:
                # Earlier turns are retained; the most recent turn is checked below.
                pass
            final_kind = 'metadata'
            continue
        if record.get('type') == 'event_msg':
            if not isinstance(item, dict):
                raise ValueError('malformed_event')
            if item.get('type') == 'user_message':
                if not isinstance(item.get('message'), str):
                    raise ValueError('nontext_owner_event')
                sources.append({'source_id':rid+':owner-event','role':'user','text':item['message']})
                final_kind = 'user'
            # Other lifecycle event strings are not content blocks.
            continue
        role, text, uses, results, modalities = _parts(record)
        if modalities:
            raise ValueError('nontext_evidence')
        if text and role in ('user', 'assistant'):
            sources.append({'source_id': rid + ':text', 'role': role, 'text': text})
            final_kind = role
        for j, call in enumerate(uses):
            cid = call.get('call_id')
            if not isinstance(cid, str) or not cid or cid in calls:
                raise ValueError('invalid_call_id')
            calls.add(cid)
            sources.append({'source_id': rid + ':call:%d' % j, 'role': 'assistant',
                            'text': json.dumps(call, ensure_ascii=False, sort_keys=True)})
            final_kind = 'call'
        for j, receipt in enumerate(results):
            cid = receipt.get('call_id')
            if cid not in calls or cid in receipts:
                raise ValueError('orphan_or_duplicate_receipt')
            receipts.add(cid)
            sources.append({'source_id': rid + ':result:%d' % j, 'role': 'tool',
                            'text': json.dumps(receipt, ensure_ascii=False, sort_keys=True)})
            final_kind = 'receipt'
    contexts = [r['payload'] for r in records if r.get('type') == 'turn_context' and isinstance(r.get('payload'), dict)]
    if contexts and payload.get('turn_id') and contexts[-1].get('turn_id') and contexts[-1]['turn_id'] != payload['turn_id']:
        raise ValueError('foreign_turn')
    if calls != receipts:
        raise ValueError('unresolved_tool_calls')
    reply = payload.get('last_assistant_message')
    if isinstance(reply, str) and reply:
        last = next((s for s in reversed(sources) if s['role'] == 'assistant'), None)
        if not last or last['text'] != reply:
            if final_kind == 'assistant':
                raise ValueError('stale_reply_boundary')
            sources.append({'source_id': 'stop:last_assistant_message', 'role': 'assistant', 'text': reply})
    if not sources or sources[-1]['role'] != 'assistant' or not any(s['role'] == 'user' for s in sources):
        raise ValueError('missing_user_or_final_reply')
    packet = make_packet(sources, {'complete': True, 'truncated': False,
                                  'completeness_kind': 'supplied_source_read',
                                  'ancestry': 'explicit' if any('parentUuid' in r or 'parent_id' in r for r in records) else 'source_order_only',
                                  'scope': 'all supplied visible transcript records; no semantic-completeness guarantee'})
    if len(json.dumps(packet, ensure_ascii=False)) > MAX_INPUT:
        raise ValueError('evidence_over_budget')
    return packet


def packet_to_candidate(packet):
    """Lossless text partition for the experimental six-field classifier input.

    This projection has its own benchmark; old compact-state scores cannot be
    attributed to it. No source text is summarized or silently shortened.
    """
    sources = packet['sources']
    users = [s['text'] for s in sources if s['role'] == 'user']
    assistants = [s for s in sources if s['role'] == 'assistant']
    last = assistants[-1]
    return {'context': '\n\n'.join(s['text'] for s in assistants[:-1] if ':call:' not in s['source_id']),
            'request': users[-1] if users else '',
            'standing_approval': '\n\n'.join(users[:-1]),
            'tools': '\n\n'.join(s['text'] for s in assistants[:-1] if ':call:' in s['source_id']),
            'tool_results': '\n\n'.join(s['text'] for s in sources if s['role'] == 'tool'),
            'reply': last['text']}


def _read_full_packet(payload):
    # Subagents need their own task/authorization lineage; unsupported until tested.
    if payload.get('hook_event_name') != 'Stop':
        raise ValueError('unsupported_event')
    path = Path(payload.get('transcript_path', '')).expanduser()
    if not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ValueError('missing_or_oversized_transcript')
    data = path.read_bytes()
    if len(data) > MAX_FILE:
        raise ValueError('oversized_transcript')
    records = [json.loads(line) for line in data.splitlines() if line.strip()]
    return packet_from_records(records, payload), path, hashlib.sha256(data).hexdigest()


def read_packet(payload):
    try:
        return _read_full_packet(payload)
    except ValueError as exc:
        if str(exc) not in {'missing_or_oversized_transcript','oversized_transcript','evidence_over_budget',
                            'nontext_evidence','invalid_call_id','missing_or_branched_ancestry',
                            'synthetic_or_sidechain_scope'}:
            raise
        from .session_packet import read_session_packet
        return read_session_packet(payload)


def preflight(payload, packet):
    if os.environ.get('REALSTOP_VERIFY_CHILD'):
        return 'recursive_child'
    if payload.get('stop_hook_active') is not False:
        return 'active_or_unknown_stop_hook'
    if payload.get('permission_mode') == 'plan':
        return 'plan_mode'
    users = [s['text'] for s in packet['sources'] if s['role'] == 'user']
    if not users or STOP.fullmatch(users[-1]) or STOP.fullmatch(re.split(r'[.!\n]', users[-1], maxsplit=1)[0]):
        return 'explicit_stop_or_missing_request'
    if payload.get('budget_exhausted') is True:
        return 'budget_exhausted'
    return None


class TurnStore:
    """Cross-process single-flight and one intervention per turn, persisted privately."""
    def __init__(self, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / 'confirmed.sqlite3'
        self.db = sqlite3.connect(self.path, timeout=1)
        os.chmod(self.path, 0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS decisions (turn TEXT PRIMARY KEY, digest TEXT, created REAL, status TEXT)')
        self.db.commit()

    def claim(self, turn, digest):
        with self.db:
            self.db.execute('DELETE FROM decisions WHERE created < ?', (time.time()-30*86400,))
            try:
                self.db.execute('INSERT INTO decisions VALUES(?,?,?,?)', (turn, digest, time.time(), 'pending'))
                return True
            except sqlite3.IntegrityError:
                return False

    def finish(self, turn, status):
        with self.db:
            self.db.execute('UPDATE decisions SET status=? WHERE turn=?', (status, turn))

    def close(self):
        self.db.close()


def evaluate_packet(payload, packet, *, provider, mode='shadow', verifier=verify_packet, timeout_s=30,
                    detector=None):
    reason = preflight(payload, packet)
    if reason:
        return {'output': {}, 'status': reason, 'verifier_called': False}
    if mode not in ('shadow', 'enforce'):
        return {'output': {}, 'status': 'invalid_mode', 'verifier_called': False}
    screening = None
    if detector is not None:
        try:
            screening = detector(packet_to_candidate(packet))
            from .candidate_screen import _valid_result
            if not _valid_result(screening):
                screening = None
        except Exception:
            screening = None
        if screening is not None and screening['experimental_flag'] is False:
            return {'output': {}, 'status': 'screen_negative', 'verifier_called': False, 'screen': screening}
        # An unavailable/oversized screen defers to the verifier, not a silent
        # negative. The independent verifier remains the only intervention gate.
    verdict = verifier(packet, provider, {'codex': 'gpt-6.1-sol', 'claude': 'sonnet'}.get(provider, ''), timeout_s)
    confirmed = verdict.get('verdict') == 'CONFIRM_QUIT'
    # Never relay an unchecked arbitrary transport response into the parent.
    from .native_verifier import validate_response
    checked = validate_response(packet, packet_hash(packet), {k: verdict.get(k) for k in ('verdict','action','references','packet_hash')})
    confirmed = confirmed and checked['verdict'] == 'CONFIRM_QUIT'
    output = {}
    if confirmed and mode == 'enforce':
        output = {'decision': 'block', 'reason':
                  'Evidence review found a potentially unfinished authorized step: ' + checked['action'] +
                  '\nRe-check it against the current task and evidence. Continue only if it is still authorized and feasible. '
                  'Do not override a user stop, budget limit, denied operation, approval requirement, or active background callback. '
                  'If already complete or genuinely blocked, explain the evidence and stop.'}
    return {'output': output, 'status': checked['verdict'], 'verifier_called': True,
            'would_block': confirmed, 'validation_reason': verdict.get('reason'), 'screen': screening}


# Only fixed labels may leave the process; exception messages can contain evidence.
_PARSER_REASONS = frozenset("""missing_transcript malformed_record foreign_session compacted_scope_requires_review
synthetic_or_sidechain_scope duplicate_source missing_or_branched_ancestry foreign_session_metadata
malformed_turn_context malformed_event nontext_owner_event nontext_evidence invalid_call_id
orphan_or_duplicate_receipt foreign_turn unresolved_tool_calls stale_reply_boundary
missing_user_or_final_reply evidence_over_budget unsupported_event missing_or_oversized_transcript
oversized_transcript missing_boundary transcript_limit compacted_scope nontext_owner
missing_owner_or_unresolved_calls stale_or_incomplete_turn missing_session""".split())


def _telemetry_id(value):
    # Identifiers are untrusted hook input, not a channel for arbitrary content.
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) else None


def _write_telemetry(path, event):
    if not path:
        return
    try:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.parent.stat().st_mode & 0o077:
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(target, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(event, separators=(',', ':')) + '\n').encode())
        finally:
            os.close(fd)
    except Exception:
        pass  # Telemetry is never allowed to affect the Stop hook.


def _timestamp():
    return datetime.now(timezone.utc).isoformat()

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--provider', choices=['claude', 'codex'], required=True)
    p.add_argument('--mode', choices=['shadow', 'enforce'], default='shadow')
    p.add_argument('--timeout', type=float, default=30)
    p.add_argument('--routing', choices=['verifier', 'cascade'], default='verifier')
    p.add_argument('--screen-url', default='http://127.0.0.1:8787/screen')
    p.add_argument('--state-dir', default=str(Path.home()/'.realstop'/'confirmed'))
    args = p.parse_args(argv)
    output, store = {}, None
    started, clock = _timestamp(), time.monotonic()
    event = {'provider': args.provider, 'start': started, 'session_id': None,
             'turn_id': None, 'digest': None, 'status': 'exception',
             'parser_error': None, 'verifier_called': False, 'emitted_block': False,
             'invocation_id': hashlib.sha256((str(os.getpid())+'|'+started).encode()).hexdigest(),
             'phase': 'start'}
    try:
        payload = json.load(sys.stdin)
        event['session_id'] = _telemetry_id(payload.get('session_id'))
        event['turn_id'] = _telemetry_id(payload.get('turn_id'))
        _write_telemetry(os.environ.get('REALSTOP_TELEMETRY_PATH'), dict(event))
        paused = os.environ.get('REALSTOP_CONFIRMED_OFF') == '1' or (Path(args.state_dir)/'paused').exists()
        legacy_control = Path.home()/'.claude'/'realstop'/'control.json'
        if legacy_control.exists():
            control = json.loads(legacy_control.read_text())
            paused = paused or control.get('paused') is True
            once = control.get('once', {}).get(payload.get('session_id'))
            paused = paused or (isinstance(once, (int,float)) and time.time()-once < 3600)
        if paused:
            event['status'] = 'paused'
            return
        if os.environ.get('REALSTOP_VERIFY_CHILD'):
            event['status'] = 'recursive_child'
            return
        if payload.get('stop_hook_active') is not False:
            event['status'] = 'active_or_unknown_stop_hook'
            return
        packet, source, digest = read_packet(payload)
        event['digest'] = digest
        sid = payload.get('session_id')
        if not isinstance(sid, str) or not sid:
            raise ValueError('missing_session')
        users = [s for s in packet['sources'] if s['role'] == 'user']
        turn = payload.get('turn_id') or packet_hash({'users': users})
        key = hashlib.sha256((args.provider+'|'+sid+'|'+str(turn)).encode()).hexdigest()
        store = TurnStore(args.state_dir)
        reason = preflight(payload, packet)
        if reason is not None:
            event['status'] = reason
        elif not store.claim(key, packet_hash(packet)):
            event['status'] = 'duplicate'
        else:
            detector = None
            if args.routing == 'cascade':
                from .candidate_screen import screen
                detector = lambda evidence: screen(evidence, endpoint=args.screen_url)
            # Mark at the call boundary, including verifier exceptions.
            def observed_verifier(*a, **kw):
                event['verifier_called'] = True
                return verify_packet(*a, **kw)
            result = evaluate_packet(payload, packet, provider=args.provider, mode=args.mode,
                                     timeout_s=args.timeout, detector=detector, verifier=observed_verifier)
            current_digest = hashlib.sha256()
            with source.open('rb') as f:
                for chunk in iter(lambda:f.read(1024*1024),b''):
                    current_digest.update(chunk)
            if current_digest.hexdigest() != digest:
                result = {'output': {}, 'status': 'source_changed_during_verification'}
            output = result['output']
            status = result['status']
            event['status'] = status if status in {'CONFIRM_QUIT', 'LEGITIMATE_STOP', 'ABSTAIN', 'CONTINUE', 'INSUFFICIENT_EVIDENCE', 'screen_negative',
                                                   'source_changed_during_verification', 'invalid_mode'} else 'other_verdict'
            store.finish(key, status)
    except Exception as exc:
        # Missing capability or damaged evidence must not hold the parent turn open.
        output = {}
        if isinstance(exc, json.JSONDecodeError):
            event['parser_error'] = 'invalid_json'
        elif isinstance(exc, ValueError) and str(exc) in _PARSER_REASONS:
            event['parser_error'] = str(exc)
    finally:
        if store is not None:
            store.close()
        event['phase'] = 'end'
        event['end'] = _timestamp()
        event['duration_ms'] = round((time.monotonic() - clock) * 1000, 3)
        event['emitted_block'] = output.get('decision') == 'block'
        _write_telemetry(os.environ.get('REALSTOP_TELEMETRY_PATH'), event)
        print(json.dumps(output))



if __name__ == '__main__':
    main()
