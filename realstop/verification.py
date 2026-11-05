"""Pure shadow-only verifier contract; no networking or authority is granted."""
from __future__ import annotations
import hashlib
import json
import math
import time
from typing import Any, Callable

CONFIRM_QUIT = 'CONFIRM_QUIT'
LEGITIMATE_STOP = 'LEGITIMATE_STOP'
INSUFFICIENT_EVIDENCE = 'INSUFFICIENT_EVIDENCE'
VERDICTS = {CONFIRM_QUIT, LEGITIMATE_STOP, INSUFFICIENT_EVIDENCE}


def canonical_hash(value: Any) -> str:
    """Hash JSON data deterministically, rejecting non-JSON and nonfinite values."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def snapshot_digest(snapshot: dict) -> str:
    return canonical_hash({k: v for k, v in snapshot.items() if k != 'content_sha256'})


def abstain(reason: str) -> dict:
    return {'verdict': INSUFFICIENT_EVIDENCE, 'reason': reason, 'action': None, 'references': {}}


def validate_response(snapshot: dict, response: Any, *, session_id: str, turn_id: str,
                      snapshot_hash: str, current_hash: str) -> dict:
    """Validate structural references; exact quotations are not proof of entailment."""
    try:
        if not isinstance(snapshot, dict) or snapshot.get('session_id') != session_id or snapshot.get('turn_id') != turn_id:
            return abstain('foreign_turn')
        if snapshot.get('schema_version') != 'realstop.evidence.snapshot.v1' or not isinstance(snapshot.get('coverage'), dict) or snapshot['coverage'].get('complete') is not True:
            return abstain('incomplete_or_unknown_evidence')
        digest = snapshot_digest(snapshot)
        if snapshot.get('content_sha256') != digest:
            return abstain('invalid_snapshot_hash')
        if digest != snapshot_hash or digest != current_hash:
            return abstain('stale_snapshot')
        if not isinstance(response, dict) or set(response) != {'verdict', 'action', 'references', 'snapshot_hash'}:
            return abstain('malformed_response')
        verdict = response['verdict']
        if verdict not in VERDICTS or response['snapshot_hash'] != digest:
            return abstain('malformed_response')
        refs = response['references']
        if not isinstance(refs, dict) or set(refs) - {'obligation', 'authorization', 'unfinished', 'feasibility'}:
            return abstain('malformed_references')
        if verdict == CONFIRM_QUIT and set(refs) != {'obligation', 'authorization', 'unfinished', 'feasibility'}:
            return abstain('missing_references')
        sources = snapshot.get('sources')
        if not isinstance(sources, list) or any(not isinstance(s, dict) for s in sources):
            return abstain('missing_sources')
        by_id = {s.get('source_id'): s for s in sources}
        if len(by_id) != len(sources) or any(not isinstance(k, str) or not k for k in by_id):
            return abstain('invalid_sources')
        for role, ref in refs.items():
            if not isinstance(ref, dict) or set(ref) != {'source_id', 'quote'}:
                return abstain('malformed_references')
            source = by_id.get(ref['source_id']) if isinstance(ref['source_id'], str) else None
            quote = ref['quote']
            if not source or not isinstance(quote, str) or not quote.strip() or len(quote) > 1000:
                return abstain('invalid_reference')
            if not isinstance(source.get('text'), str) or quote not in source['text']:
                return abstain('unsupported_quote')
            # Authorization can only cite an observed owner instruction, not a tool or model.
            if role == 'authorization':
                # A tool-result record can also have role=user. Its aggregated source
                # text is not an owner statement; require the quote in user prose itself.
                contexts = snapshot.get('context', [])
                if not isinstance(contexts, list) or not any(
                    isinstance(c, dict) and c.get('source_id') == ref['source_id']
                    and c.get('role') == 'user' and isinstance(c.get('text'), str)
                    and quote in c['text'] for c in contexts
                ):
                    return abstain('invalid_authority_source')
        action = response['action']
        if verdict == CONFIRM_QUIT:
            if not isinstance(action, str) or not action.strip() or len(action) > 500:
                return abstain('invalid_action')
        elif action is not None:
            return abstain('unexpected_action')
        return {'verdict': verdict, 'reason': 'validated_references_only', 'action': action, 'references': refs}
    except (TypeError, ValueError, KeyError):
        return abstain('malformed_response')


def verify(snapshot: dict, transport: Callable[[dict, float], Any] | None = None, *,
           session_id: str, turn_id: str, snapshot_hash: str, current_hash: str,
           timeout_s: float = 1.0, external: bool = False, allow_external: bool = False,
           clock: Callable[[], float] = time.monotonic) -> dict:
    """Call a trusted injected transport once. Its deadline is cooperative, not a sandbox.

    External transports are denied by default. Callers must enforce network and process
    isolation separately; a Python callable cannot be forcibly interrupted safely here.
    """
    try:
        if not isinstance(snapshot, dict) or snapshot.get('session_id') != session_id or snapshot.get('turn_id') != turn_id:
            return abstain('foreign_turn')
        if snapshot.get('schema_version') != 'realstop.evidence.snapshot.v1' or not isinstance(snapshot.get('coverage'), dict) or snapshot['coverage'].get('complete') is not True:
            return abstain('incomplete_or_unknown_evidence')
        if snapshot_digest(snapshot) != snapshot_hash or snapshot.get('content_sha256') != snapshot_hash or snapshot_hash != current_hash:
            return abstain('stale_snapshot')
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
            return abstain('invalid_timeout')
        if external and not allow_external:
            return abstain('external_disabled')
        if transport is None:
            return abstain('transport_unavailable')
        start = clock()
        # Copy through JSON so a scripted transport cannot mutate the caller's snapshot.
        payload = json.loads(json.dumps(snapshot, allow_nan=False))
        result = transport(payload, start + timeout_s)
        if clock() > start + timeout_s:
            return abstain('timeout')
        return validate_response(snapshot, result, session_id=session_id, turn_id=turn_id,
                                 snapshot_hash=snapshot_hash, current_hash=current_hash)
    except TimeoutError:
        return abstain('timeout')
    except Exception:
        return abstain('transport_error')
