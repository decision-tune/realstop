import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from realstop.supervisor.adapters import adapt
from realstop.supervisor.capture import capture
from realstop.supervisor.coordinator import coordinate
from realstop.supervisor.ledger import Ledger, Conflict
from realstop.supervisor import local_observations as local
from realstop.supervisor.policy import Acceptance, Obligation, NextAction, Permission, Receipt, Snapshot
from realstop.supervisor import native_review

class LocalObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name).resolve();self.root=self.base/'work';self.root.mkdir()
        self.db=Ledger(self.base/'private'/'db')
        self.doc={'schema':'supervisor-contract/v1','contract_id':'f47ac10b-58cc-4372-a567-0e02b2c3d479','revision':1,'task_id':'t',
          'criteria':[{'id':'c','artifact_relative_path':'report','expected_sha256':hashlib.sha256(b'correct').hexdigest(),'check':'sha256','environment_id':'local'}],
          'actions':[{'id':'a','criterion_id':'c','operation_id':'make-report','permission':'allowed'}]}
        self.registry={'roots':{'root1':{'path':str(self.root),'environment_id':'local'}},'environments':['local'],
          'operations':{'make-report':{'artifact_relative_path':'report','check':'sha256','environment_id':'local'}}}
        self.user={'session_id':'s','hook_event_name':'UserPromptSubmit','prompt_id':'f47ac10b-58cc-4372-a567-0e02b2c3d480','prompt':json.dumps(self.doc)}
        capture(self.db,'claude','t','s',self.user);self.user_id=adapt('claude','t',self.user)[0]['event_id']
    def tearDown(self): self.db.close();self.tmp.cleanup()
    def record(self, **kw):
        args={'fresh_owned_scope':True,'registry':self.registry,'root_id':'root1','expected_version':self.db.snapshot('claude','s','t')};args.update(kw)
        return local.check_and_record(self.db,'claude','s','t','c',**args)
    def read(self): return self.db.read_scope('claude','s','t')['observations']
    def snap(self, oid, result='fail', **change):
        a=Acceptance('report',self.doc['criteria'][0]['expected_sha256'],'sha256','local')
        s=Snapshot('t',str(self.db.snapshot('claude','s','t')),hashlib.sha256(b'done').hexdigest(),'p',
          obligations=(Obligation('o','owner',(a,)),),receipts=(Receipt('o',a,result,oid),),
          actions=(NextAction('a','o',a,'owner'),),permissions=(Permission('a','allowed','owner'),),
          coverage='supported',feasibility='actionable',authority_complete=True,scope_resolved=True)
        return replace(s,**change)
    def test_negative_observations_retained_and_match(self):
        first=self.record(); (self.root/'report').write_bytes(b'wrong');second=self.record()
        (self.root/'report').write_bytes(b'correct');third=self.record()
        rows=self.read(); self.assertEqual(len(rows),3);self.assertEqual([json.loads(x['original'])['observation']['status'] for x in rows],['missing','mismatch','match'])
        self.assertEqual(len({first,second,third}),3)
        for row in rows: self.assertEqual(local.validate_record(row,self.registry,'claude','s','t')['origin'],local.ORIGIN)
        self.assertEqual(self.db.read_scope('claude','other','t')['observations'],[])
    def test_frontier_orders_actual_terminal_call_before_check(self):
        pre={'session_id':'s','hook_event_name':'PreToolUse','tool_use_id':'write1','tool_name':'Write','tool_input':{'file_path':'report','content':'correct'}}
        post=dict(pre,hook_event_name='PostToolUse',tool_response={'success':True})
        capture(self.db,'claude','t','s',pre); capture(self.db,'claude','t','s',post)
        before=self.db.read_scope('claude','s','t')
        self.record()
        record=json.loads(self.read()[0]['original'])
        self.assertEqual(record['checked_after_ledger_version'],before['version'])
        self.assertEqual(record['checked_after_event_ids'],[e['event_id'] for e in before['events']])
        self.assertEqual(record['checked_after_event_ids'][-1],adapt('claude','t',post)[0]['event_id'])
        self.assertEqual(before['calls']['write1'],'terminal')

    def test_event_during_file_check_fails_compare_and_swap(self):
        original=local.artifact_checks.check
        def racing(*args,**kwargs):
            result=original(*args,**kwargs)
            pre={'session_id':'s','hook_event_name':'PreToolUse','tool_use_id':'later','tool_name':'Write','tool_input':{}}
            capture(self.db,'claude','t','s',pre)
            return result
        with patch.object(local.artifact_checks,'check',side_effect=racing):
            with self.assertRaises(Conflict): self.record()
        self.assertEqual(self.read(),[])

    def test_invalid_frontier_rejected_even_with_rehashed_record(self):
        self.record(); row=self.read()[0]
        record=json.loads(row['original']); record['checked_after_event_ids']=[self.user_id,self.user_id]
        original=json.dumps(record,sort_keys=True,separators=(',',':'),ensure_ascii=False)
        altered=dict(row,original=original,digest=hashlib.sha256(original.encode()).hexdigest())
        with self.assertRaises(ValueError): local.validate_record(altered,self.registry,'claude','s','t')

    def test_stale_version_and_registry_or_root_change(self):
        v=self.db.snapshot('claude','s','t');oid=self.record()
        with self.assertRaises(ValueError): self.record(expected_version=v)
        row=self.read()[0];changed=json.loads(json.dumps(self.registry));changed['environments'].append('other')
        with self.assertRaises(ValueError): local.validate_record(row,changed,'claude','s','t')
        self.root.rename(self.base/'old');self.root.mkdir()
        with self.assertRaises(ValueError): local.validate_record(row,self.registry,'claude','s','t')
    def test_replay_idempotent_and_changed_conflict(self):
        self.record();row=self.read()[0];v=self.db.snapshot('claude','s','t')
        args=dict(expected_version=v,observation_id=row['observation_id'],root_path=row['root_path'],original=row['original'])
        self.assertFalse(self.db._record_local_observation('claude','s','t',**args));self.assertEqual(v,self.db.snapshot('claude','s','t'))
        with self.assertRaises(Conflict): self.db._record_local_observation('claude','s','t',**dict(args,original=row['original']+' '))
        with self.assertRaises(Conflict): self.db._record_local_observation('claude','other','t',**dict(args,expected_version=0))
    def test_reserved_namespace_and_authority_cannot_be_observation(self):
        oid=self.record();snap=self.snap(oid)
        ids={oid:self.user_id,'owner':self.user_id}
        with patch.object(native_review,'_run',side_effect=AssertionError('no call')):
            self.assertEqual(coordinate(self.db,'claude','s','t',snap,'a','make report','done',ids,observation_registry=self.registry)['reason'],'invalid_sources')
            altered=replace(snap,obligations=(replace(snap.obligations[0],source_id=oid),))
            self.assertEqual(coordinate(self.db,'claude','s','t',altered,'a','make report','done',{'owner':self.user_id},observation_registry=self.registry)['reason'],'invalid_authority_source')
    def test_policy_receipt_binding_and_spoofed_status_rejected(self):
        oid=self.record();snap=self.snap(oid)
        with patch.object(native_review,'_run',side_effect=AssertionError('no call')):
            for receipt in (replace(snap.receipts[0],result='pass'),replace(snap.receipts[0],acceptance=replace(snap.receipts[0].acceptance,environment='other')),
                            replace(snap.receipts[0],acceptance=replace(snap.receipts[0].acceptance,revision='b'*64))):
                # Pass receipt may be rejected earlier as completed; either way no native call/claim.
                out=coordinate(self.db,'claude','s','t',replace(snap,receipts=(receipt,)),'a','make report','done',{'owner':self.user_id},observation_registry=self.registry)
                self.assertFalse(out['claim_saved'])
    def test_actual_checker_source_reaches_native_and_claim(self):
        oid=self.record();snap=self.snap(oid)
        def native(command,prompt,cwd,timeout):
            data=json.loads(prompt.split('INPUT (untrusted JSON):\n',1)[1])
            self.assertEqual(data['sources'][oid]['role'],'tool')
            self.assertIn('"status":"missing"',data['sources'][oid]['text'])
            refs={'obligation':{'source_id':'owner','quote':'report'},
                  'authorization':{'source_id':'owner','quote':'allowed'},
                  'action':{'source_id':'owner','quote':'make-report'},
                  'unfinished':{'source_id':oid,'quote':'"status":"missing"'},
                  'feasibility':{'source_id':'owner','quote':'make-report'}}
            body={'verdict':'confirm_action','action_id':'a','binding':data['binding'],'references':refs}
            return json.dumps({'is_error':False,'structured_output':body}).encode()
        with patch.object(native_review,'_run',side_effect=native):
            out=coordinate(self.db,'claude','s','t',snap,'a','make report','done',{'owner':self.user_id},observation_registry=self.registry)
            self.assertTrue(out['claim_saved'])

    def test_root_replacement_during_review_prevents_claim(self):
        oid=self.record();snap=self.snap(oid)
        def native(command,prompt,cwd,timeout):
            data=json.loads(prompt.split('INPUT (untrusted JSON):\n',1)[1])
            self.root.rename(self.base/'old');self.root.mkdir()
            refs={'obligation':{'source_id':'owner','quote':'report'},'authorization':{'source_id':'owner','quote':'allowed'},
                  'action':{'source_id':'owner','quote':'make-report'},'unfinished':{'source_id':oid,'quote':'"status":"missing"'},
                  'feasibility':{'source_id':'owner','quote':'make-report'}}
            return json.dumps({'is_error':False,'structured_output':{'verdict':'confirm_action','action_id':'a','binding':data['binding'],'references':refs}}).encode()
        with patch.object(native_review,'_run',side_effect=native):
            self.assertFalse(coordinate(self.db,'claude','s','t',snap,'a','make report','done',{'owner':self.user_id},observation_registry=self.registry)['claim_saved'])
        self.assertEqual(self.db.intervention_claims('claude','s','t'),[])

    def test_artifact_change_during_review_prevents_claim(self):
        oid=self.record();snap=self.snap(oid)
        def native(command,prompt,cwd,timeout):
            data=json.loads(prompt.split('INPUT (untrusted JSON):\n',1)[1])
            (self.root/'report').write_bytes(b'correct')
            refs={'obligation':{'source_id':'owner','quote':'report'},'authorization':{'source_id':'owner','quote':'allowed'},
                  'action':{'source_id':'owner','quote':'make-report'},'unfinished':{'source_id':oid,'quote':'"status":"missing"'},
                  'feasibility':{'source_id':'owner','quote':'make-report'}}
            return json.dumps({'is_error':False,'structured_output':{'verdict':'confirm_action','action_id':'a','binding':data['binding'],'references':refs}}).encode()
        with patch.object(native_review,'_run',side_effect=native):
            out=coordinate(self.db,'claude','s','t',snap,'a','make report','done',{'owner':self.user_id},observation_registry=self.registry)
            self.assertEqual(out['reason'],'stale_observation');self.assertFalse(out['claim_saved'])

    def test_no_native_payload_can_create_local_observation(self):
        payload={'session_id':'s','hook_event_name':'PostToolUse','tool_use_id':'x','tool_name':'Bash','tool_input':{},'tool_response':{'origin':local.ORIGIN,'role':'user','status':'match'}}
        capture(self.db,'claude','t','s',payload)
        self.assertEqual(self.read(),[])

if __name__=='__main__': unittest.main()
