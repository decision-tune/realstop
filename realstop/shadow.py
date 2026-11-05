"""Offline shadow comparison of injected arms. Never changes a hook or controls an agent."""
from __future__ import annotations
import copy
import threading
import time
from typing import Any, Callable
from .verification import CONFIRM_QUIT, INSUFFICIENT_EVIDENCE, snapshot_digest, verify

GATES = ('explicit_stop', 'permission_blocked', 'budget_exhausted', 'background_callback')


def gate_reason(policy: dict) -> str | None:
    """Unknown safety state fails closed; inputs must come from an authoritative caller."""
    if not isinstance(policy, dict) or any(type(policy.get(k)) is not bool for k in (*GATES, 'snapshot_current', 'evidence_complete')):
        return 'unknown_policy_state'
    if not policy['snapshot_current'] or not policy['evidence_complete']:
        return 'stale_or_incomplete_evidence'
    for key in GATES:
        if policy[key]:
            return key
    return None


class ShadowReplay:
    """In-memory idempotence only; not a cross-process/production safety mechanism."""
    def __init__(self, *, max_nudges: int = 1, clock: Callable[[], float] = time.monotonic):
        if type(max_nudges) is not int or not 0 <= max_nudges <= 5:
            raise ValueError('invalid nudge cap')
        self.max_nudges = max_nudges
        self.clock = clock
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str], dict] = {}
        self._counts: dict[tuple[str, str], int] = {}

    def replay(self, snapshot: dict, policy: dict, *, current_hash: str,
               detector: Callable[[dict], Any] | None = None,
               verifier: Callable[[dict, float], Any] | None = None,
               legacy: Callable[[dict], Any] | None = None,
               rich: Callable[[dict], Any] | None = None,
               verifier_external: bool = False, allow_external: bool = False,
               policy_hash: str = '', timeout_s: float = 1.0) -> dict:
        # Serialize duplicate deliveries; no asynchronous side effects or retries.
        if not isinstance(snapshot, dict):
            raise ValueError('snapshot must be a mapping')
        session, turn = snapshot.get('session_id'), snapshot.get('turn_id')
        if not isinstance(session, str) or not session or not isinstance(turn, str) or not turn:
            raise ValueError('session and turn required')
        key = (session, turn)
        with self._lock:
            try:
                digest = snapshot_digest(snapshot)
            except (TypeError, ValueError):
                return {'status': 'invalid_snapshot', 'mode': 'shadow', 'arms': {}}
            if key in self._seen:
                result = copy.deepcopy(self._seen[key])
                if result['snapshot_hash'] != digest:
                    return {'status': 'conflicting_duplicate', 'mode': 'shadow', 'arms': {}}
                result['duplicate'] = True
                return result
            reason = gate_reason(policy)
            if digest != snapshot.get('content_sha256') or digest != current_hash:
                reason = 'stale_or_invalid_snapshot'
            if reason:
                arms = {name: {'availability': 'gated', 'disposition': 'gate_allow',
                               'raw_disposition': 'not_invoked', 'gate': reason, 'payload': None,
                               'payload_sha256': None, 'latency_seconds': 0.0, 'cost': None,
                               'policy_hash': policy_hash}
                        for name in ('shipped_hook', 'richer_evidence', 'detector_only',
                                     'verifier_alone', 'detector_plus_verifier')}
                result = {'status': 'complete', 'mode': 'shadow', 'snapshot_hash': digest,
                          'session_id': session, 'turn_id': turn, 'gate': reason,
                          'duplicate': False, 'arms': arms}
                self._seen[key] = copy.deepcopy(result)
                return result
            arms: dict[str, dict] = {}
            def invoke(name: str, adapter: Callable | None) -> dict:
                payload = copy.deepcopy(snapshot)
                observed_payload = copy.deepcopy(payload)
                if adapter is None:
                    return {'availability': 'unsupported', 'disposition': 'unsupported', 'payload': None,
                            'payload_sha256': None, 'latency_seconds': 0.0, 'cost': None, 'policy_hash': policy_hash}
                t = self.clock()
                try:
                    decision = adapter(payload)
                    if not isinstance(decision, bool):
                        raise ValueError('adapter must return bool')
                    disposition = 'would_nudge' if decision else 'would_allow'
                except Exception:
                    decision, disposition = None, 'abstain'
                return {'availability': 'injected', 'disposition': disposition, 'decision': decision,
                        'payload': observed_payload, 'payload_sha256': snapshot_digest(observed_payload),
                        'latency_seconds': max(0.0, self.clock()-t), 'cost': None, 'policy_hash': policy_hash}
            # Even unsupported actual-hook arms are explicit: injected adapters are not called shipped behavior.
            arms['shipped_hook'] = invoke('shipped_hook', legacy)
            arms['richer_evidence'] = invoke('richer_evidence', rich)
            arms['detector_only'] = invoke('detector_only', detector)
            t = self.clock()
            v = verify(snapshot, verifier, session_id=session, turn_id=turn, snapshot_hash=digest,
                       current_hash=current_hash, timeout_s=timeout_s, external=verifier_external,
                       allow_external=allow_external, clock=self.clock)
            verifier_arm = {'availability': 'injected' if verifier else 'unsupported',
                            'disposition': 'would_nudge' if v['verdict'] == CONFIRM_QUIT else ('abstain' if v['verdict'] == INSUFFICIENT_EVIDENCE else 'would_allow'),
                            'decision': v, 'payload': copy.deepcopy(snapshot), 'payload_sha256': digest,
                            'latency_seconds': max(0.0, self.clock()-t), 'cost': None, 'policy_hash': policy_hash}
            arms['verifier_alone'] = verifier_arm
            detector_decision = arms['detector_only'].get('decision')
            cascade = ('would_allow' if detector_decision is False else
                       'would_nudge' if detector_decision is True and v['verdict'] == CONFIRM_QUIT else
                       'would_allow' if detector_decision is True and v['verdict'] == 'LEGITIMATE_STOP' else 'abstain')
            arms['detector_plus_verifier'] = {'availability': 'injected' if detector and verifier else 'unsupported',
                'disposition': cascade if detector and verifier else 'unsupported', 'payload': copy.deepcopy(snapshot),
                'payload_sha256': digest, 'latency_seconds': arms['detector_only']['latency_seconds'] + (verifier_arm['latency_seconds'] if detector_decision is True else 0.0),
                'routed': detector_decision is True, 'hypothetical_verifier_calls': 1 if detector_decision is True else 0,
                'verifier_result_source': 'shared_cached_verifier_alone_result' if detector_decision is True else None,
                'cost': None, 'policy_hash': policy_hash}
            # Gates dominate all arm suggestions, but retain raw disposition as audit evidence.
            for arm in arms.values():
                arm['raw_disposition'] = arm['disposition']
                if reason and arm['availability'] != 'unsupported':
                    arm['disposition'] = 'gate_allow'
                    arm['gate'] = reason
            if not reason and self._counts.get(key, 0) >= self.max_nudges:
                for arm in arms.values():
                    if arm['disposition'] == 'would_nudge':
                        arm['disposition'] = 'cap_allow'
            elif not reason and any(a['disposition'] == 'would_nudge' for a in arms.values()):
                self._counts[key] = self._counts.get(key, 0) + 1
            result = {'status': 'complete', 'mode': 'shadow', 'snapshot_hash': digest,
                      'session_id': session, 'turn_id': turn, 'gate': reason, 'duplicate': False, 'arms': arms}
            self._seen[key] = copy.deepcopy(result)
            return result
