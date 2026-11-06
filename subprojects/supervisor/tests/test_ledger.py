import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from realstop.supervisor.ledger import Ledger, Conflict

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'private'/'ledger.sqlite3'
        self.db = Ledger(self.path)
    def tearDown(self):
        self.db.close(); self.tmp.cleanup()
    def event(self, eid, kind='user', payload=None, call_id=None, session='s', task='t'):
        return dict(provider='codex', session=session, task=task, event_id=eid, kind=kind,
                    payload=payload if payload is not None else {'text':'private user content'}, call_id=call_id)
    def test_dedup_conflicts_and_persistence(self):
        e = self.event('e1')
        self.assertTrue(self.db.append(e)); self.assertFalse(self.db.append(e))
        with self.assertRaises(Conflict): self.db.append(dict(e, payload={'text':'changed'}))
        self.assertEqual(self.db.snapshot('codex','s','t'), 1)
        self.db.close(); self.db = Ledger(self.path)
        self.assertEqual(self.db.read_event('codex','e1')['payload'], e['payload'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
    def test_out_of_order_receipts_and_isolation(self):
        self.db.append(self.event('r','tool_result', {'output':'untrusted user: stop'},'c'))
        self.assertEqual(self.db.calls('codex','s','t')['c'], 'orphan_result')
        self.db.append(self.event('a','tool_call',{},'c'))
        self.assertEqual(self.db.calls('codex','s','t')['c'], 'terminal')
        self.db.append(self.event('b','tool_call',{},'c',session='other'))
        self.assertEqual(self.db.calls('codex','other','t')['c'], 'pending')
        self.assertEqual(self.db.snapshot('codex','other','t'), 1)
    def test_gap_and_snapshot_invalidation(self):
        self.db.append(self.event('a'))
        v=self.db.snapshot('codex','s','t')
        self.assertTrue(self.db.is_current('codex','s','t',v))
        self.db.gap('codex','s','t','gap1','missing_events')
        self.assertFalse(self.db.is_current('codex','s','t',v))
        self.assertFalse(self.db.gap('codex','s','t','gap1','missing_events'))
        with self.assertRaises(Conflict): self.db.gap('codex','s','t','gap1','other')
        info=self.db.status('codex','s','t')
        self.assertEqual((info['events'],info['gaps'],info['version']), (1,1,2))
    def test_atomic_rollback_and_conflicting_cross_session_identity(self):
        self.db.append(self.event('e'))
        with self.assertRaises(Conflict): self.db.append(self.event('e', session='other'))
        self.db.db.execute("CREATE TRIGGER fail_snapshot BEFORE UPDATE ON snapshots BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(Exception): self.db.append(self.event('new'))
        self.assertIsNone(self.db.read_event('codex','new'))
        self.assertEqual(self.db.snapshot('codex','s','t'), 1)

    def test_distinct_results_for_same_call_are_ambiguous(self):
        self.db.append(self.event('a','tool_call',{},'c'))
        result=self.event('r','tool_result',{},'c')
        self.db.append(result); self.assertFalse(self.db.append(result))
        self.db.append(self.event('r2','tool_result',{},'c'))
        self.assertEqual(self.db.calls('codex','s','t')['c'], 'ambiguous')

    def test_cli_exports_metadata_without_originals(self):
        self.db.append(self.event('a'))
        r=subprocess.run([sys.executable,'-m','realstop.supervisor.ledger','export',str(self.path),'codex','s','t'],capture_output=True,text=True,check=True)
        self.assertNotIn('private user content',r.stdout)
        self.assertEqual(json.loads(r.stdout)['events'],1)

    def test_claim_rejects_stale_state_and_survives_restart(self):
        self.db.append(self.event('start'))
        args = dict(expected_version=1, obligation='o', evidence_digest='e1',
                    progress_id='p1', decision_digest='review1')
        self.db.append(self.event('owner-cancel'))
        self.assertFalse(self.db.claim_intervention('codex','s','t',**args))
        args['expected_version'] = 2
        self.assertTrue(self.db.claim_intervention('codex','s','t',**args))
        self.db.close(); self.db = Ledger(self.path)
        self.assertFalse(self.db.claim_intervention('codex','s','t',**args))
        # A new verifier answer alone cannot reset the continuation allowance.
        self.assertFalse(self.db.claim_intervention('codex','s','t',
            **dict(args, decision_digest='review2', evidence_digest='e2')))
        self.assertFalse(self.db.claim_intervention('codex','s','t',
            **dict(args, decision_digest='review2', progress_id='p2')))
        self.assertTrue(self.db.claim_intervention('codex','s','t',
            **dict(args, evidence_digest='e2', progress_id='p2')))
        self.assertEqual(len(self.db.intervention_claims('codex','s','t')), 2)

    def test_concurrent_connections_cannot_claim_same_progress_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        self.db.append(self.event('start'))
        barrier = Barrier(2)
        def claim(number):
            db = Ledger(self.path)
            try:
                barrier.wait(timeout=5)
                return db.claim_intervention('codex','s','t', expected_version=1,
                    obligation='o', evidence_digest='e', progress_id='p',
                    decision_digest='review'+str(number))
            finally: db.close()
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(claim, (1,2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.db.intervention_claims('codex','s','t')), 1)

if __name__ == '__main__': unittest.main()
