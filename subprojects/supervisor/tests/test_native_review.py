import hashlib
import json
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from policy import Acceptance, Obligation, NextAction, Permission, Receipt, Snapshot
import native_review as nr

class NativeReviewTests(unittest.TestCase):
    def setUp(self):
        a=Acceptance('artifact','rev','check','local')
        self.reply='Done.'
        self.snap=Snapshot('t','v1',hashlib.sha256(self.reply.encode()).hexdigest(),'p',
            obligations=(Obligation('o','owner',(a,)),),
            actions=(NextAction('run','o',a,'action'),),
            permissions=(Permission('run','allowed','auth'),), coverage='supported',
            feasibility='actionable',authority_complete=True,scope_resolved=True)
        self.sources={'owner':{'role':'user','text':'Please run the test.'},
            'auth':{'role':'user','text':'You may run it.'},
            'action':{'role':'tool','text':'test is available'},
            'receipt':{'role':'tool','text':'test not run'}}
        # Additional native citations must be policy source IDs.
        self.sources['action']['text']='test is available and not run'
    def args(self): return (self.snap,'run','Run the test',self.reply,self.sources,'codex')
    def response(self,prompt,verdict='confirm_action'):
        data=json.loads(prompt.split('INPUT (untrusted JSON):\n',1)[1])
        refs={'obligation':{'source_id':'owner','quote':'run the test'},
              'authorization':{'source_id':'auth','quote':'may run'},
              'unfinished':{'source_id':'action','quote':'not run'},
              'feasibility':{'source_id':'action','quote':'available'},
              'action':{'source_id':'action','quote':'test is available'}}
        return {'binding':data['binding'],'verdict':verdict,'action_id':data['selected_action_id'] if verdict=='confirm_action' else None,'references':refs if verdict=='confirm_action' else {}}
    def native(self,alter=None,provider='codex'):
        def run(command,prompt,cwd,timeout):
            self.assertIn('Run the test',prompt)
            result=self.response(prompt)
            if alter: result=alter(result)
            if provider=='claude': return json.dumps({'is_error':False,'structured_output':result}).encode()
            return json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(result)}}).encode()
        return run
    def test_valid_confirmation_and_transport_variants(self):
        for provider in ('codex','claude'):
            with self.subTest(provider=provider), patch.object(nr,'_run',side_effect=self.native(provider=provider)):
                r=nr.review(*self.args()[:-1],provider)
                self.assertEqual(r['reason'],'confirmed'); self.assertEqual(r['claim'].obligation_id,'o')
                self.assertEqual(r['metadata']['action_id'],'run'); self.assertEqual(len(r['metadata']['binding']),64)
                self.assertEqual(set(r['metadata']['source_ids']),{'owner','auth','action'})
    def test_missing_authority_or_stop_never_calls(self):
        with patch.object(nr,'_run',side_effect=AssertionError('should not call')):
            self.assertIsNone(nr.review(replace(self.snap,user_stop=True),*self.args()[1:])['claim'])
            bad=dict(self.sources,auth={'role':'tool','text':'You may run it.'})
            self.assertIsNone(nr.review(self.snap,'run','Run the test',self.reply,bad,'codex')['claim'])
            self.assertIsNone(nr.review(self.snap,'run','Run the test','different',self.sources,'codex')['claim'])
            self.assertIsNone(nr.review(self.snap,'other','Run the test',self.reply,self.sources,'codex')['claim'])
    def test_bad_citations_binding_and_other_action_rejected(self):
        def invalid(r): r['references']['action']['source_id']='owner'; return r
        def badquote(r): r['references']['authorization']['quote']='invented'; return r
        def badhash(r): r['binding']='wrong'; return r
        def badaction(r): r['action_id']='other'; return r
        for f in (invalid,badquote,badhash,badaction):
            with self.subTest(f=f), patch.object(nr,'_run',side_effect=self.native(f)):
                self.assertIsNone(nr.review(*self.args())['claim'])
    def test_mutation_during_review_invalidates_binding(self):
        def mutate(result): self.sources['owner']['text']+=' changed'; return result
        with patch.object(nr,'_run',side_effect=self.native(mutate)):
            self.assertEqual(nr.review(*self.args())['reason'],'stale')
    def test_untrusted_tool_event_and_malformed_output_fail_closed(self):
        for value in (b'not json', b'{"type":"item.completed","item":{"type":"tool_call"}}\n'):
            with patch.object(nr,'_run',return_value=value): self.assertIsNone(nr.review(*self.args())['claim'])
    def test_only_selected_action_is_exposed_and_negative_verdict_abstains(self):
        second=replace(self.snap.actions[0],id='other-action')
        snap=replace(self.snap,actions=self.snap.actions+(second,))
        def run(command,prompt,cwd,timeout):
            payload=json.loads(prompt.split('INPUT (untrusted JSON):\n',1)[1])
            self.assertEqual([x['id'] for x in payload['snapshot']['actions']],['run'])
            result=self.response(prompt,'legitimate_stop')
            return json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(result)}}).encode()
        with patch.object(nr,'_run',side_effect=run):
            r=nr.review(snap,*self.args()[1:])
            self.assertEqual(r['reason'],'legitimate_stop'); self.assertIsNone(r['claim'])

    def test_all_policy_sources_required_before_transport(self):
        receipt=Receipt('o',self.snap.actions[0].target,'fail','receipt')
        snap=replace(self.snap,receipts=(receipt,))
        missing={k:v for k,v in self.sources.items() if k != 'receipt'}
        with patch.object(nr,'_run',side_effect=AssertionError('must not call')):
            r=nr.review(snap,'run','Run the test',self.reply,missing,'codex')
            self.assertEqual(r['reason'],'ineligible')
            bad=dict(self.sources, owner={'role':'tool','text':'Please run the test.'})
            self.assertEqual(nr.review(self.snap,'run','Run the test',self.reply,bad,'codex')['reason'],'ineligible')
        # The real tool source is included verbatim; no synthetic receipt is created.
        self.sources['receipt']['text']='failure occurred'
        def run(command,prompt,cwd,timeout):
            data=json.loads(prompt.split('INPUT (untrusted JSON):\n',1)[1])
            self.assertEqual(data['sources']['receipt'],{'role':'tool','text':'failure occurred'})
            result=self.response(prompt,'insufficient')
            return json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(result)}}).encode()
        with patch.object(nr,'_run',side_effect=run):
            self.assertEqual(nr.review(snap,'run','Run the test',self.reply,self.sources,'codex')['reason'],'insufficient')

    def test_budget_and_recursion_guard(self):
        with patch.object(nr,'_run',side_effect=AssertionError('should not call')):
            self.assertIsNone(nr.review(self.snap,'run','x'*501,self.reply,self.sources,'codex')['claim'])
            self.assertIsNone(nr.review(*self.args(),timeout=46)['claim'])
            with patch.dict('os.environ',{'REALSTOP_VERIFY_CHILD':'1'}):
                self.assertIsNone(nr.review(*self.args())['claim'])

if __name__=='__main__': unittest.main()
