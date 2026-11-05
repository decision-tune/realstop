import concurrent.futures
import unittest
from realstop.shadow import ShadowReplay
from test_verification import answer, fixture


def safe():
    return {'explicit_stop': False, 'permission_blocked': False, 'budget_exhausted': False,
            'background_callback': False, 'snapshot_current': True, 'evidence_complete': True}


class ShadowTests(unittest.TestCase):
    def test_verifier_alone_runs_when_detector_says_no(self):
        s = fixture(); calls = []
        def v(payload, deadline):
            calls.append(payload); return answer(payload)
        x = ShadowReplay().replay(s, safe(), current_hash=s['content_sha256'], detector=lambda _: False, verifier=v)
        self.assertEqual(len(calls), 1)
        self.assertEqual(x['arms']['verifier_alone']['disposition'], 'would_nudge')
        self.assertEqual(x['arms']['detector_plus_verifier']['disposition'], 'would_allow')
        self.assertFalse(x['arms']['detector_plus_verifier']['routed'])
        self.assertEqual(x['arms']['detector_plus_verifier']['hypothetical_verifier_calls'], 0)
        self.assertEqual(x['arms']['detector_plus_verifier']['latency_seconds'], x['arms']['detector_only']['latency_seconds'])
        self.assertEqual(x['arms']['shipped_hook']['availability'], 'unsupported')
        self.assertEqual(x['arms']['richer_evidence']['availability'], 'unsupported')
        self.assertEqual(x['mode'], 'shadow')

    def test_gates_override_and_do_not_call_transports(self):
        for k in ('explicit_stop', 'permission_blocked', 'budget_exhausted', 'background_callback'):
            s = fixture(); p = safe(); p[k] = True; calls = []
            x = ShadowReplay().replay(s, p, current_hash=s['content_sha256'], detector=lambda _: calls.append(1), verifier=lambda *_: calls.append(2))
            self.assertEqual(x['gate'], k); self.assertFalse(calls)
            self.assertEqual(x['arms']['detector_plus_verifier']['disposition'], 'gate_allow')
        p = safe(); p['snapshot_current'] = None
        self.assertEqual(ShadowReplay().replay(fixture(), p, current_hash=fixture()['content_sha256'])['gate'], 'unknown_policy_state')

    def test_duplicate_concurrency_is_idempotent_and_conflicts_fail(self):
        s = fixture(); replay = ShadowReplay(); calls = []
        def run():
            return replay.replay(s, safe(), current_hash=s['content_sha256'], detector=lambda _: True, verifier=lambda x, _: (calls.append(1), answer(x))[1])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda _: run(), range(4)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(r['duplicate'] for r in results), 3)
        changed = fixture(); changed['sources'][0]['text'] += ' extra'; changed['content_sha256'] = __import__('realstop.verification', fromlist=['snapshot_digest']).snapshot_digest(changed)
        self.assertEqual(replay.replay(changed, safe(), current_hash=changed['content_sha256'])['status'], 'conflicting_duplicate')

    def test_error_timeout_and_cap_never_force(self):
        s = fixture(); x = ShadowReplay(max_nudges=0).replay(s, safe(), current_hash=s['content_sha256'], detector=lambda _: True, verifier=lambda x, _: answer(x))
        self.assertEqual(x['arms']['detector_plus_verifier']['disposition'], 'cap_allow')
        self.assertEqual(x['arms']['detector_plus_verifier']['hypothetical_verifier_calls'], 1)
        self.assertEqual(x['arms']['detector_plus_verifier']['verifier_result_source'], 'shared_cached_verifier_alone_result')
        y = ShadowReplay().replay(s, safe(), current_hash=s['content_sha256'], detector=lambda _: True, verifier=lambda *_: (_ for _ in ()).throw(RuntimeError()))
        self.assertEqual(y['arms']['detector_plus_verifier']['disposition'], 'abstain')

if __name__ == '__main__': unittest.main()
