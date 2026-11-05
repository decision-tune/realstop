"""Installer CLI boundary: preserve unrelated configuration and guarded recovery."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/install-confirmed-hooks.py'


class InstallConfirmedHooks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='confirmed hooks ')
        self.root = Path(self.temporary.name)
        self.source = self.root/'source tree'; package = self.source/'realstop'; package.mkdir(parents=True)
        (package/'__init__.py').write_text('')
        (package/'system_hook.py').write_text('print("isolated release launcher works")\n')
        (package/'native_verifier.py').write_text('# fixture module\n')
        (self.source/'private.key').write_text('must not copy')
        self.destination = self.root/'immutable releases'/'realstop rc1'
        self.claude = self.root/'claude settings.json'; self.codex = self.root/'codex hooks.json'
        self.unrelated = {'type':'command','command':'/other/hook --keep','custom': {'enabled':True}}
        legacy_c = {'type':'command','command':'/usr/bin/python3 -I /home/test/.claude/realstop/no-quit.py','timeout':20}
        legacy_x = {'type':'command','command':'/usr/bin/python3 -I /home/test/.codex/realstop/codex-stop.py','timeout':5}
        self.original_c = {'theme':'dark','hooks':{'Stop':[{'hooks':[self.unrelated,legacy_c,self.unrelated], 'matcher':'*'}], 'SubagentStop':[{'hooks':[legacy_c,self.unrelated]}], 'SessionStart':[{'hooks':[{'command':'/home/test/.claude/realstop/learn.py'}]}]}}
        self.original_x = {'other':17,'hooks':{event:[{'hooks':[self.unrelated,legacy_x]}] for event in ('Stop','PostToolUse','UserPromptSubmit')}}
        self.claude.write_text(json.dumps(self.original_c)); self.codex.write_text(json.dumps(self.original_x))
        self.before = {self.claude:self.claude.read_bytes(), self.codex:self.codex.read_bytes()}

    def tearDown(self): self.temporary.cleanup()

    def invoke(self, *extra, success=True):
        cmd = [sys.executable,str(SCRIPT),'--source-root',str(self.source),'--destination',str(self.destination),'--claude-settings',str(self.claude),'--codex-hooks',str(self.codex),*extra]
        result = subprocess.run(cmd,capture_output=True,text=True,env=dict(os.environ, HOME=str(self.root)))
        if success: self.assertEqual(result.returncode,0,result.stderr)
        else: self.assertNotEqual(result.returncode,0)
        return result

    def test_dry_run_preserves_bytes_and_apply_preserves_positions_and_other_hooks(self):
        self.invoke(); self.assertFalse(self.destination.exists())
        for path,data in self.before.items(): self.assertEqual(path.read_bytes(),data)
        plan=json.loads(self.invoke('--apply').stdout)
        c=json.loads(self.claude.read_text()); x=json.loads(self.codex.read_text())
        stops=c['hooks']['Stop'][0]['hooks']
        self.assertEqual(stops[0],self.unrelated); self.assertEqual(stops[2],self.unrelated)
        self.assertEqual(c['hooks']['SessionStart'],self.original_c['hooks']['SessionStart'])
        self.assertEqual(c['hooks']['SubagentStop'][0]['hooks'],[self.unrelated])
        for event in ('PostToolUse','UserPromptSubmit'): self.assertEqual(x['hooks'][event][0]['hooks'],[self.unrelated])
        self.assertEqual(x['other'],17); self.assertEqual(c['theme'],'dark')
        launched=subprocess.run(shlex.split(stops[1]['command']),capture_output=True,text=True)
        self.assertEqual(launched.returncode,0,launched.stderr)
        self.assertEqual(launched.stdout.strip(),'isolated release launcher works')
        self.assertFalse((self.destination/'private.key').exists())
        receipt=json.loads(Path(plan['receipt']).read_text())
        for item in receipt['changes']: self.assertEqual(Path(item['backup']).stat().st_mode & 0o777,0o600)
        self.assertEqual(self.claude.stat().st_mode & 0o777,0o600)

    def test_idempotence_and_immutable_destination(self):
        self.invoke('--apply'); before=self.claude.read_bytes(); stamp=self.destination.stat().st_mtime_ns
        result=json.loads(self.invoke('--apply').stdout)
        self.assertEqual(result['status'],'already_installed'); self.assertEqual(self.claude.read_bytes(),before)
        self.assertEqual(self.destination.stat().st_mtime_ns,stamp)
        (self.source/'realstop/native_verifier.py').write_text('changed\n')
        self.assertIn('nonempty release destination differs',self.invoke('--apply',success=False).stderr)
        self.assertEqual(self.claude.read_bytes(),before)

    def test_upgrade_previous_immutable_release_and_rollback(self):
        self.destination = self.root/'.local/share/realstop/releases/0.2.0rc1'
        self.invoke('--apply')
        old_c, old_x = self.claude.read_bytes(), self.codex.read_bytes()
        old_release = self.destination
        self.destination = old_release.parent/'0.2.0rc1-monitor1'
        plan = json.loads(self.invoke('--apply').stdout)
        for path in (self.claude, self.codex):
            self.assertIn(str(self.destination/'launcher.py'), path.read_text())
            self.assertIn('/other/hook --keep', path.read_text())
        restored = subprocess.run([sys.executable, plan['rollback']], capture_output=True, text=True)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(self.claude.read_bytes(), old_c)
        self.assertEqual(self.codex.read_bytes(), old_x)

    def test_modified_prior_release_is_rejected(self):
        self.destination = self.root/'.local/share/realstop/releases/0.2.0rc1'
        self.invoke('--apply')
        (self.destination/'launcher.py').write_text('tampered')
        old_c = self.claude.read_bytes()
        self.destination = self.destination.parent/'next'
        self.assertIn('unrecognized RealStop', self.invoke('--apply', success=False).stderr)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.claude.read_bytes(), old_c)

    def test_unknown_realstop_command_rejected_before_any_mutation(self):
        c=json.loads(self.claude.read_text());c['hooks']['Stop'][0]['hooks'][1]['command']='/unknown/realstop/custom.py'
        self.claude.write_text(json.dumps(c)); before=self.claude.read_bytes()
        self.assertIn('unrecognized RealStop',self.invoke('--apply',success=False).stderr)
        self.assertFalse(self.destination.exists());self.assertEqual(self.claude.read_bytes(),before)

    def test_rollback_restores_exact_bytes_and_refuses_future_edits_atomically(self):
        plan=json.loads(self.invoke('--apply').stdout); rollback=plan['rollback']
        changed=self.claude.read_bytes(); installed_codex=self.codex.read_bytes(); self.codex.write_text('{"future":"edit"}')
        rejected=subprocess.run([sys.executable,rollback],capture_output=True,text=True)
        self.assertNotEqual(rejected.returncode,0);self.assertIn('current file changed',rejected.stderr)
        self.assertEqual(self.claude.read_bytes(),changed)
        self.codex.write_bytes(installed_codex)
        restored=subprocess.run([sys.executable,rollback],capture_output=True,text=True)
        self.assertEqual(restored.returncode,0,restored.stderr)
        for path,data in self.before.items():self.assertEqual(path.read_bytes(),data)
        repeated=subprocess.run([sys.executable,rollback],capture_output=True,text=True)
        self.assertEqual(repeated.returncode,0,repeated.stderr)

    def test_corrupt_backup_cannot_partially_restore_other_configuration(self):
        plan=json.loads(self.invoke('--apply').stdout)
        receipt=json.loads(Path(plan['receipt']).read_text())
        before_c=self.claude.read_bytes();before_x=self.codex.read_bytes()
        Path(receipt['changes'][-1]['backup']).write_text('corrupt')
        result=subprocess.run([sys.executable,plan['rollback']],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertIn('Backup integrity mismatch',result.stderr)
        self.assertEqual(self.claude.read_bytes(),before_c);self.assertEqual(self.codex.read_bytes(),before_x)

    def test_nonempty_directory_without_runtime_is_not_an_idempotent_release(self):
        (self.destination/'empty-child').mkdir(parents=True)
        self.assertIn('nonempty release destination differs',self.invoke('--apply',success=False).stderr)
        for path,data in self.before.items():self.assertEqual(path.read_bytes(),data)


if __name__=='__main__':unittest.main()
