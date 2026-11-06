import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from realstop.supervisor import native_review
from realstop.supervisor.ledger import Ledger
from realstop.supervisor.pilot import validate_contract, _write_private, launch
from realstop.supervisor.pilot_hook import handle


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); base=Path(self.tmp.name).resolve()
        self.root=base/'work'; self.root.mkdir(); self.private=base/'private'; self.private.mkdir(mode=0o700)
        self.session=str(uuid.uuid4()); self.prompt_id=str(uuid.uuid4()); self.text='hello\n'
        self.doc={'schema':'supervisor-contract/v2','revision':2,'contract_id':str(uuid.uuid4()),'task_id':'task',
          'task_instruction':'Write hello followed by a newline to answer.txt.',
          'criteria':[{'id':'c','artifact_relative_path':'answer.txt','expected_sha256':hashlib.sha256(self.text.encode()).hexdigest(),'check':'sha256','environment_id':'local'}],
          'actions':[{'id':'a','criterion_id':'c','operation_id':'write-answer','permission':'allowed'}]}
        self.raw=json.dumps(self.doc); st=self.root.stat()
        self.state={'session':self.session,'task':'task','contract':self.raw,'workspace':str(self.root),'root_identity':[st.st_dev,st.st_ino]}
        _write_private(self.private/'state.json',self.state)
    def tearDown(self): self.tmp.cleanup()
    def payload(self,name,**fields):
        return dict(session_id=self.session,hook_event_name=name,**fields)
    def user(self,**fields):
        payload=self.payload('UserPromptSubmit',prompt=self.raw,prompt_id=self.prompt_id); payload.update(fields)
        return handle(self.private,payload)
    def stop(self,**fields):
        payload=self.payload('Stop',stop_hook_active=False,last_assistant_message='Done.'); payload.update(fields)
        return handle(self.private,payload)
    def mock_native(self,command,prompt,cwd,timeout):
        data=json.loads(prompt.split('INPUT (untrusted JSON):\n',1)[1])
        local=next(x for x in data['sources'] if x.startswith('local-observation:'))
        refs={'obligation':{'source_id':'owner','quote':'answer.txt'},'authorization':{'source_id':'owner','quote':'allowed'},
              'action':{'source_id':'owner','quote':'write-answer'},'unfinished':{'source_id':local,'quote':'"status":"missing"'},
              'feasibility':{'source_id':'owner','quote':'write-answer'}}
        return json.dumps({'is_error':False,'structured_output':{'verdict':'confirm_action','action_id':'a','binding':data['binding'],'references':refs}}).encode()
    def test_v2_validation(self):
        self.assertEqual(validate_contract(self.raw),self.doc)
        for key,value in [('criteria',None),('actions',[]),('task_instruction','')]:
            doc=dict(self.doc); doc[key]=value
            with self.assertRaises((ValueError,TypeError)): validate_contract(json.dumps(doc))
        with self.assertRaises(ValueError): validate_contract('{"schema":1,"schema":2}')
    def test_real_stop_one_attempt_and_claim(self):
        self.user()
        with patch.object(native_review,'_run',side_effect=self.mock_native) as native:
            self.assertEqual(self.stop()['decision'],'block')
            self.assertEqual(self.stop(),{})
            self.assertEqual(native.call_count,1)
        self.assertEqual(len(list(self.private.glob('stop-*.json'))),2)
        result=json.loads((self.private/'attempt-result.json').read_text()); self.assertTrue(result['claim_saved'])
    def test_completed_and_active_stops_do_not_call(self):
        self.user(); (self.root/'answer.txt').write_text(self.text)
        with patch.object(native_review,'_run',side_effect=AssertionError):
            self.assertEqual(self.stop(),{}); self.assertEqual(self.stop(stop_hook_active=True),{})
        self.assertFalse((self.private/'attempt-reserved').exists())
    def test_extra_user_missing_id_and_compaction_abstain(self):
        for scenario in ('extra','missing','compact'):
            with self.subTest(scenario=scenario):
                self.tearDown(); self.setUp()
                self.user(**({'prompt_id':None} if scenario=='missing' else {}))
                if scenario=='extra': self.user()
                if scenario=='compact': handle(self.private,self.payload('PreCompact'))
                with patch.object(native_review,'_run',side_effect=AssertionError): self.assertEqual(self.stop(),{})
    def test_tool_paths_and_symlinks(self):
        self.user()
        def pre(path, name='Write'):
            return handle(self.private,self.payload('PreToolUse',tool_use_id=str(uuid.uuid4()),tool_name=name,
                tool_input={'file_path':str(path),'content':self.text}))
        self.assertEqual(pre(self.root/'answer.txt'),{})
        self.assertEqual(pre(self.root/'other')['hookSpecificOutput']['permissionDecision'],'deny')
        self.assertEqual(pre(self.root/'answer.txt','Bash')['hookSpecificOutput']['permissionDecision'],'deny')
        (self.root/'answer.txt').symlink_to('/tmp')
        self.assertEqual(pre(self.root/'answer.txt')['hookSpecificOutput']['permissionDecision'],'deny')
    def test_native_tool_protocol_and_uncertain_stop(self):
        self.user(); tool={'tool_use_id':'call','tool_name':'Write','tool_input':{'file_path':str(self.root/'answer.txt'),'content':self.text}}
        self.assertEqual(handle(self.private,self.payload('PreToolUse',**tool)),{})
        self.assertEqual(handle(self.private,self.payload('PostToolUse',tool_response={'filePath':str(self.root/'answer.txt')},**tool)),{})
        with patch.object(native_review,'_run',side_effect=self.mock_native): self.assertEqual(self.stop()['decision'],'block')
    def test_hook_cli_uses_trusted_event_for_failures(self):
        cases=[('PreToolUse',b'{',True),('PreToolUse',b'x'*(1024*1024+1),True),
               ('PreToolUse',json.dumps(self.payload('Stop')).encode(),True),
               ('Stop',b'{',False),('Stop',b'x'*(1024*1024+1),False)]
        for event,raw,denied in cases:
            with self.subTest(event=event, size=len(raw)):
                proc=subprocess.run([sys.executable,'-m','realstop.supervisor.pilot_hook',
                    '--state',str(self.private),'--event',event],input=raw,capture_output=True,check=True)
                output=json.loads(proc.stdout)
                self.assertEqual(output.get('hookSpecificOutput',{}).get('permissionDecision')=='deny',denied)
        # Inaccessible state also cannot turn a PreToolUse failure into allow.
        proc=subprocess.run([sys.executable,'-m','realstop.supervisor.pilot_hook',
            '--state',str(self.private/'missing'),'--event','PreToolUse'],input=b'{}',capture_output=True,check=True)
        self.assertEqual(json.loads(proc.stdout)['hookSpecificOutput']['permissionDecision'],'deny')

    def test_launcher_interrupt_stops_process_group_and_returns_130(self):
        process=Mock(pid=424242)
        process.wait.side_effect=[KeyboardInterrupt(),subprocess.TimeoutExpired('claude',5),
                                  subprocess.TimeoutExpired('claude',5),0]
        with tempfile.TemporaryDirectory() as tmp:
            runroot=Path(tmp)/'pilot'; runroot.mkdir()
            with patch('realstop.supervisor.pilot.tempfile.mkdtemp',return_value=str(runroot)), \
                 patch('realstop.supervisor.pilot.subprocess.Popen',return_value=process), \
                 patch('realstop.supervisor.pilot.os.killpg') as kill:
                self.assertEqual(launch(self.raw),130)
                self.assertEqual([call.args for call in kill.call_args_list],[(424242,signal.SIGINT),
                    (424242,signal.SIGTERM),(424242,signal.SIGKILL)])
                self.assertTrue((runroot/'private'/'settings.json').is_file())
                settings=json.loads((runroot/'private'/'settings.json').read_text())
                for event,groups in settings['hooks'].items():
                    self.assertIn('--event '+event,groups[0]['hooks'][0]['command'])

    def test_reservation_remains_consumed_after_exception(self):
        self.user()
        with patch('realstop.supervisor.pilot_hook.coordinate',side_effect=RuntimeError):
            with self.assertRaises(RuntimeError): self.stop()
        with patch.object(native_review,'_run',side_effect=AssertionError): self.assertEqual(self.stop(),{})


if __name__=='__main__': unittest.main()
