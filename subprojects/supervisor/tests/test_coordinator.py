import hashlib
import json
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from adapters import adapt
from capture import capture
from coordinator import coordinate
from ledger import Ledger
from policy import Acceptance, Obligation, NextAction, Permission, Snapshot
import native_review

class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'private'/'db'; self.db=Ledger(self.path)
        self.user={'session_id':'s','hook_event_name':'UserPromptSubmit','prompt_id':'f47ac10b-58cc-4372-a567-0e02b2c3d479','prompt':'Please run tests. You may run tests.'}
        self.call={'session_id':'s','hook_event_name':'PreToolUse','tool_use_id':'c','tool_name':'Bash','tool_input':{'command':'check availability'}}
        self.result={'session_id':'s','hook_event_name':'PostToolUse','tool_use_id':'c','tool_name':'Bash','tool_input':{'command':'check availability'},'tool_response':'tests available, not run'}
        for item in (self.user,self.call,self.result): capture(self.db,'claude','t','s',item)
        self.ids={name:adapt('claude','t',value)[0]['event_id'] for name,value in [('owner',self.user),('action',self.result)]}
        self.reply='Done.'; a=Acceptance('artifact','rev','tests','local')
        self.snap=Snapshot('t',str(self.db.snapshot('claude','s','t')),hashlib.sha256(self.reply.encode()).hexdigest(),'p',
          obligations=(Obligation('o','owner',(a,)),),actions=(NextAction('run','o',a,'action'),),
          permissions=(Permission('run','allowed','owner'),),coverage='supported',feasibility='actionable',authority_complete=True,scope_resolved=True)
    def tearDown(self): self.db.close(); self.tmp.cleanup()
    def args(self): return (self.db,'claude','s','t',self.snap,'run','Run tests',self.reply,self.ids)
    def fake(self, callback=None):
        def run(command,prompt,cwd,timeout):
            data=json.loads(prompt.split('INPUT (untrusted JSON):\n',1)[1]); src=data['sources']
            assert src['owner']['role']=='user' and src['action']['role']=='tool'
            refs={'obligation':{'source_id':'owner','quote':'run tests'},'authorization':{'source_id':'owner','quote':'may run tests'},
                  'unfinished':{'source_id':'action','quote':'not run'},'feasibility':{'source_id':'action','quote':'available'},
                  'action':{'source_id':'action','quote':'tests available'}}
            if callback: callback()
            return json.dumps({'is_error':False,'structured_output':{'verdict':'confirm_action','action_id':'run','binding':data['binding'],'references':refs}}).encode()
        return run
    def test_capture_to_review_claim_and_retry_suppression(self):
        with patch.object(native_review,'_run',side_effect=self.fake()) as run:
            result=coordinate(*self.args()); self.assertTrue(result['claim_saved']); self.assertEqual(result['reason'],'claim_saved')
            self.assertEqual(len(self.db.intervention_claims('claude','s','t')),1)
            self.assertFalse(coordinate(*self.args())['claim_saved']); self.assertEqual(run.call_count,1)
    def test_new_owner_during_review_invalidates_atomic_claim(self):
        def change():
            other=Ledger(self.path)
            try: capture(other,'claude','t','s',dict(self.user,prompt_id='f47ac10b-58cc-4372-a567-0e02b2c3d480',prompt='Stop now'))
            finally: other.close()
        with patch.object(native_review,'_run',side_effect=self.fake(change)):
            r=coordinate(*self.args()); self.assertFalse(r['claim_saved']); self.assertEqual(r['reason'],'stale_or_duplicate')
        self.assertEqual(self.db.intervention_claims('claude','s','t'),[])
    def test_foreign_missing_and_tool_user_impersonation_rejected(self):
        foreign=dict(self.user,session_id='other')
        capture(self.db,'claude','t','other',foreign)
        other_id=adapt('claude','t',foreign)[0]['event_id']
        for ids in ({'owner':other_id,'action':self.ids['action']}, {'action':self.ids['action']},
                    {'owner':self.ids['action'],'action':self.ids['action']}):
            with self.subTest(ids=ids), patch.object(native_review,'_run',side_effect=AssertionError('should not call')):
                self.assertFalse(coordinate(*self.args()[:-1],ids)['claim_saved'])
    def test_tool_text_cannot_be_owner_even_with_real_owner_in_packet(self):
        ids={'owner':self.ids['action'],'action':self.ids['action'],'actual-user':self.ids['owner']}
        with patch.object(native_review,'_run',side_effect=AssertionError('should not call')):
            self.assertEqual(coordinate(*self.args()[:-1],ids)['reason'],'invalid_authority_source')

    def test_pending_call_abstains(self):
        capture(self.db,'claude','t','s',dict(self.call,tool_use_id='pending'))
        snap=replace(self.snap,version=str(self.db.snapshot('claude','s','t')))
        with patch.object(native_review,'_run',side_effect=AssertionError('should not call')):
            self.assertEqual(coordinate(*self.args()[:4],snap,*self.args()[5:])['reason'],'incomplete_capture')

    def test_missing_history_gap_and_unfinished_call_abstain(self):
        self.db.gap('claude','s','t','legacy','unknown_prior_history')
        snap=replace(self.snap,version=str(self.db.snapshot('claude','s','t')))
        with patch.object(native_review,'_run',side_effect=AssertionError('should not call')):
            self.assertEqual(coordinate(*self.args()[:4],snap,*self.args()[5:])['reason'],'incomplete_capture')
    def test_unmapped_contrary_tool_result_is_sent_to_native(self):
        call=dict(self.call,tool_use_id='counter')
        result=dict(self.result,tool_use_id='counter',tool_response='contrary: tests already passed')
        capture(self.db,'claude','t','s',call); capture(self.db,'claude','t','s',result)
        snap=replace(self.snap,version=str(self.db.snapshot('claude','s','t')))
        def run(command,prompt,cwd,timeout):
            data=json.loads(prompt.split('INPUT (untrusted JSON):\n',1)[1])
            texts=[v['text'] for v in data['sources'].values() if v['role']=='tool']
            self.assertTrue(any('contrary: tests already passed' in text for text in texts))
            return json.dumps({'is_error':False,'structured_output':{'verdict':'insufficient','action_id':None,'binding':data['binding'],'references':{}}}).encode()
        with patch.object(native_review,'_run',side_effect=run) as mocked:
            response=coordinate(*self.args()[:4],snap,*self.args()[5:])
            self.assertEqual(response['reason'],'insufficient'); self.assertEqual(mocked.call_count,1)
            self.assertFalse(response['claim_saved'])

    def test_new_unmapped_owner_refuses_review(self):
        capture(self.db,'claude','t','s',dict(self.user,prompt_id='f47ac10b-58cc-4372-a567-0e02b2c3d480'))
        snap=replace(self.snap,version=str(self.db.snapshot('claude','s','t')))
        with patch.object(native_review,'_run',side_effect=AssertionError('should not call')):
            self.assertEqual(coordinate(*self.args()[:4],snap,*self.args()[5:])['reason'],'incomplete_authority')

if __name__=='__main__': unittest.main()
