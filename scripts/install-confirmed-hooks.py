#!/usr/bin/env python3
"""Plan an immutable confirmed-hook release; mutate only with --apply."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import sys
import tempfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, indent=2, ensure_ascii=False)+'\n').encode()


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


ROLLBACK = '''#!/usr/bin/env python3
import hashlib,json,os,pathlib,sys,tempfile
p=pathlib.Path(__file__).with_name('receipt.json')
r=json.loads(p.read_text())
for item in r['changes']:
 target=pathlib.Path(item['path'])
 current=hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
 if current not in (item['after_sha256'],item['before_sha256']):raise SystemExit('Refusing rollback: current file changed: '+str(target))
 if item['backup'] is not None:
  data=pathlib.Path(item['backup']).read_bytes()
  if hashlib.sha256(data).hexdigest()!=item['before_sha256']:raise SystemExit('Backup integrity mismatch')
for item in r['changes']:
 target=pathlib.Path(item['path'])
 current=hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
 if current==item['before_sha256']:continue
 if current!=item['after_sha256']:raise SystemExit('Refusing rollback: current file changed: '+str(target))
 if item['backup'] is None:target.unlink();continue
 data=pathlib.Path(item['backup']).read_bytes()
 if hashlib.sha256(data).hexdigest()!=item['before_sha256']:raise SystemExit('Backup integrity mismatch')
 fd,tmp=tempfile.mkstemp(prefix='.'+target.name+'.rollback.',dir=target.parent)
 os.fchmod(fd,0o600)
 with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
 os.replace(tmp,target)
print('Restored hook configuration; immutable release retained.')
'''


def command(destination, provider, mode):
    return shlex.join([str(Path(sys.executable).absolute()), '-I', str(destination/'launcher.py'), '--provider', provider, '--mode', mode])


def own_command(text, provider, destination):
    try: tokens = shlex.split(text)
    except ValueError:
        if 'realstop' in text.lower(): raise ValueError('unrecognized RealStop command: '+text)
        return False
    expected = '/.claude/realstop/no-quit.py' if provider == 'claude' else '/.codex/realstop/codex-stop.py'
    if any(t.endswith(expected) for t in tokens):
        # Only a direct Python invocation, never a compound shell command.
        if len(tokens) in (2, 3) and tokens[-1].endswith(expected) and (len(tokens) == 2 or tokens[1] == '-I') and Path(tokens[0]).name.startswith('python'):
            return True
        raise ValueError('unrecognized RealStop command: '+text)
    # Prior immutable releases are recognized only in the standard release root,
    # with an intact manifest and the exact launcher invocation we generate.
    root = Path.home()/'.local'/'share'/'realstop'/'releases'
    if len(tokens) == 7 and tokens[0] == str(Path(sys.executable).absolute()) and tokens[1] == '-I':
        launcher = Path(tokens[2])
        release = launcher.parent
        if (launcher.name == 'launcher.py' and release.parent == root and
                re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', release.name) and
                tokens[3:5] == ['--provider', provider] and tokens[5] == '--mode' and
                tokens[6] in ('shadow', 'enforce')):
            try:
                if release.is_symlink() or not release.is_dir(): raise ValueError('invalid release')
                manifest_path = release/'release-manifest.json'
                if manifest_path.is_symlink(): raise ValueError('invalid manifest')
                manifest = json.loads(manifest_path.read_text())
                files = manifest['files']
                if manifest['schema'] != 'realstop.confirmed.release.v1' or not isinstance(files, dict): raise ValueError('invalid manifest')
                if not {'launcher.py', 'realstop/system_hook.py', 'realstop/native_verifier.py'} <= files.keys(): raise ValueError('missing runtime')
                for name, expected_hash in files.items():
                    if name != 'launcher.py' and not re.fullmatch(r'realstop/[A-Za-z0-9_]+\.py', name): raise ValueError('invalid file')
                    file = release/name
                    if file.is_symlink() or file.parent.is_symlink() or digest(file.read_bytes()) != expected_hash: raise ValueError('modified runtime')
                return True
            except (KeyError, TypeError, ValueError, OSError):
                raise ValueError('unrecognized RealStop command: '+text)
    if str(destination/'launcher.py') in tokens:
        if tokens == [str(Path(sys.executable).absolute()), '-I', str(destination/'launcher.py'), '--provider', provider, '--mode', 'shadow'] or tokens == [str(Path(sys.executable).absolute()), '-I', str(destination/'launcher.py'), '--provider', provider, '--mode', 'enforce']:
            return True
        raise ValueError('unrecognized RealStop command: '+text)
    if 'realstop' in text.lower():
        raise ValueError('unrecognized RealStop command: '+text)
    return False


def updated(original, provider, destination, mode):
    result = copy.deepcopy(original)
    hooks = result.setdefault('hooks', {})
    if not isinstance(hooks, dict): raise ValueError('hooks must be an object')
    events = ('Stop', 'SubagentStop') if provider == 'claude' else ('Stop', 'PostToolUse', 'UserPromptSubmit')
    installed = False
    for event in events:
        groups = hooks.get(event, [])
        if not isinstance(groups, list): raise ValueError('hook event must be an array')
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks'), list): raise ValueError('invalid hook group')
            retained = []
            for hook in group['hooks']:
                if not isinstance(hook, dict): raise ValueError('invalid hook')
                if own_command(str(hook.get('command', '')), provider, destination):
                    if event == 'Stop' and not installed:
                        retained.append({'type': 'command', 'command': command(destination, provider, mode), 'timeout': 45})
                        installed = True
                else: retained.append(hook)
            group['hooks'] = retained
    if not installed:
        hooks.setdefault('Stop', []).append({'hooks': [{'type': 'command', 'command': command(destination, provider, mode), 'timeout': 45}]})
    return result


def release_files(source, destination):
    files = {'realstop/'+p.name: p.read_bytes() for p in sorted((source/'realstop').glob('*.py')) if p.is_file() and not p.is_symlink()}
    if 'realstop/system_hook.py' not in files or 'realstop/native_verifier.py' not in files:
        raise ValueError('source lacks confirmed hook runtime')
    files['launcher.py'] = ("import runpy,sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parent))\nrunpy.run_module('realstop.system_hook',run_name='__main__')\n").encode()
    files['release-manifest.json'] = encoded({'schema': 'realstop.confirmed.release.v1', 'files': {name: digest(data) for name, data in files.items()}})
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir(): raise ValueError('release destination must be a real directory')
        existing = {str(p.relative_to(destination)): p.read_bytes() for p in destination.rglob('*') if p.is_file()}
        if any(p.is_symlink() for p in destination.rglob('*')): raise ValueError('symlink in immutable release')
        if any(destination.iterdir()) and existing != files: raise ValueError('nonempty release destination differs')
    return files


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root', type=Path, required=True)
    p.add_argument('--destination', type=Path, required=True)
    p.add_argument('--claude-settings', type=Path, required=True)
    p.add_argument('--codex-hooks', type=Path, required=True)
    p.add_argument('--mode', choices=['shadow', 'enforce'], default='enforce')
    p.add_argument('--apply', action='store_true')
    args = p.parse_args(argv)
    destination = args.destination.expanduser().absolute()
    try:
        files = release_files(args.source_root, destination)
        changes = []
        for provider, path in [('claude', args.claude_settings.expanduser().absolute()), ('codex', args.codex_hooks.expanduser().absolute())]:
            if path.is_symlink(): raise ValueError('configuration symlink refused: '+str(path))
            before = path.read_bytes() if path.exists() else None
            old = json.loads(before) if before is not None else {}
            if not isinstance(old, dict): raise ValueError('configuration must be an object')
            new = updated(old, provider, destination, args.mode)
            after = before if old == new else encoded(new)
            if before != after: changes.append({'path': str(path), 'before': before, 'after': after})
        plan = {'apply': args.apply, 'destination': str(destination), 'mode': args.mode, 'changed_configurations': [c['path'] for c in changes], 'release_files': {name: digest(data) for name, data in files.items()}, 'trust_configuration': 'untouched; host review may be required'}
        if not args.apply:
            print(json.dumps(plan, indent=2)); return
        # Everything is validated before any mutation. Release creation is an
        # atomic rename; an existing equal release is never rewritten.
        if not destination.exists() or not any(destination.iterdir()):
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='.'+destination.name+'.', dir=destination.parent))
            try:
                for name, data in files.items(): atomic(stage/name, data)
                if destination.exists(): destination.rmdir()
                os.replace(stage, destination)
            finally:
                if stage.exists(): shutil.rmtree(stage)
        if not changes:
            print(json.dumps(dict(plan, status='already_installed'))); return
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        receipt_dir = destination.parent / (destination.name+'.install-'+stamp)
        receipt_dir.mkdir(mode=0o700)
        receipt = {'schema': 'realstop.confirmed.install.v1', 'release': str(destination), 'changes': []}
        for i, c in enumerate(changes):
            path = Path(c['path'])
            current = path.read_bytes() if path.exists() else None
            if current != c['before']: raise ValueError('configuration changed during planning: '+str(path))
            backup = receipt_dir / ('configuration-'+str(i)+'.backup') if c['before'] is not None else None
            if backup is not None: atomic(backup, c['before'])
            receipt['changes'].append({'path': str(path), 'backup': str(backup) if backup else None, 'before_sha256': digest(c['before']) if c['before'] is not None else None, 'after_sha256': digest(c['after'])})
        atomic(receipt_dir/'receipt.json', encoded(receipt)); atomic(receipt_dir/'rollback.py', ROLLBACK.encode())
        for c in changes:
            path = Path(c['path'])
            if (path.read_bytes() if path.exists() else None) != c['before']:
                raise ValueError('configuration changed before write; use receipt rollback: '+str(path))
            atomic(path, c['after'])
        print(json.dumps(dict(plan, status='installed', receipt=str(receipt_dir/'receipt.json'), rollback=str(receipt_dir/'rollback.py')), indent=2))
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc))


if __name__ == '__main__': main()
