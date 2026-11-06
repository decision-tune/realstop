from __future__ import annotations

"""Experimental Claude fresh-workspace pilot, not a general session hook."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import uuid

from .contracts import _pairs, _path

MAX_CONTRACT = 8192
SYSTEM_PROMPT = 'Follow the user-provided JSON contract and task_instruction. Use only the available Write tool for the explicitly authorized artifact.'


def validate_contract(raw):
    if len(raw.encode('utf-8')) > MAX_CONTRACT:
        raise ValueError('contract too large')
    data = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid json')))
    if not isinstance(data, dict) or set(data) != {'schema','contract_id','revision','task_id','task_instruction','criteria','actions'}:
        raise ValueError('invalid contract fields')
    if data['schema'] != 'supervisor-contract/v2' or type(data['revision']) is not int or data['revision'] != 2:
        raise ValueError('invalid contract revision')
    if str(uuid.UUID(data['contract_id'])) != data['contract_id']:
        raise ValueError('invalid contract id')
    if not isinstance(data['task_id'], str) or not data['task_id'] or len(data['task_id']) > 128:
        raise ValueError('invalid task id')
    if not isinstance(data['task_instruction'], str) or not data['task_instruction'].strip() or len(data['task_instruction']) > 4096:
        raise ValueError('invalid instruction')
    if not isinstance(data['criteria'], list) or not isinstance(data['actions'], list) or len(data['criteria']) != 1 or len(data['actions']) != 1:
        raise ValueError('expected one criterion and action')
    c, a = data['criteria'][0], data['actions'][0]
    if not isinstance(c,dict) or set(c) != {'id','artifact_relative_path','expected_sha256','check','environment_id'}:
        raise ValueError('invalid criterion')
    if not isinstance(a,dict) or set(a) != {'id','criterion_id','operation_id','permission'}:
        raise ValueError('invalid action')
    from .contracts import _id
    for value in (c['id'],c['environment_id'],a['id'],a['criterion_id'],a['operation_id']): _id(value)
    _path(c['artifact_relative_path'])
    if c['check'] != 'sha256' or not isinstance(c['expected_sha256'],str) or len(c['expected_sha256']) != 64 or any(x not in '0123456789abcdef' for x in c['expected_sha256']):
        raise ValueError('invalid sha256')
    if a['criterion_id'] != c['id'] or a['permission'] != 'allowed':
        raise ValueError('invalid action authority')
    return data


def _write_private(path, value):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False)


def launch(raw, *, timeout=180):
    data=validate_contract(raw)
    session=str(uuid.uuid4())
    base=Path(tempfile.mkdtemp(prefix='realstop-pilot-')).resolve()
    os.chmod(base, 0o700)
    workspace=base/'workspace'; workspace.mkdir(mode=0o700)
    private=base/'private'; private.mkdir(mode=0o700)
    # Parents are created by the launcher, not inferred from the agent's input.
    target=workspace/data['criteria'][0]['artifact_relative_path']
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    st=workspace.stat()
    state={'session':session,'task':data['task_id'],'contract':raw,'workspace':str(workspace),
           'root_identity':[st.st_dev,st.st_ino], 'system_prompt':SYSTEM_PROMPT}
    _write_private(private/'state.json',state)
    package_root=str(Path(__file__).resolve().parents[2])
    script='import sys;sys.path.insert(0,'+repr(package_root)+');from realstop.supervisor.pilot_hook import main;main()'
    command=' '.join(shlex.quote(v) for v in (sys.executable,'-c',script,'--state',str(private)))
    hook=lambda name: {'matcher':'','hooks':[{'type':'command','command':command,'timeout':45}]}
    settings={'hooks':{name:[hook(name)] for name in ('UserPromptSubmit','PreToolUse','PostToolUse','PostToolUseFailure','Stop','PreCompact','PostCompact','InstructionsLoaded','SubagentStart','SubagentStop')}}
    _write_private(private/'settings.json',settings)
    args=['claude','-p','--session-id',session,'--setting-sources','','--settings',str(private/'settings.json'),
          '--restricted','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--tools','Write',
          '--allowedTools','Write','--permission-mode','acceptEdits','--no-session-persistence',
          '--disable-slash-commands','--max-turns','8','--max-budget-usd','1.00',
          '--system-prompt',SYSTEM_PROMPT,raw]
    print('Pilot workspace:',workspace,flush=True)
    print('Private records:',private,flush=True)
    try:
        process=subprocess.Popen(args,cwd=workspace,start_new_session=True)
        try: code=process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGTERM)
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL); process.wait()
            print('Pilot timed out.',file=sys.stderr); code=1
    except OSError:
        print('Pilot launch failed.',file=sys.stderr); code=1
    # An external check after the parent exits; never treat failure as completion.
    from .artifact_checks import check
    from .contracts import CriterionDeclaration
    c=data['criteria'][0]
    criterion=CriterionDeclaration(c['id'],hashlib.sha256((session+':'+c['id']).encode()).hexdigest(),c['artifact_relative_path'],c['expected_sha256'],c['check'],c['environment_id'])
    observed=check(str(workspace),criterion,c['environment_id'])
    print('Post-exit artifact check:',observed.status,flush=True)
    return code if code else (0 if observed.status == 'match' else 1)


def main():
    parser=argparse.ArgumentParser(description='Experimental Claude pilot in a new private workspace.')
    parser.add_argument('--contract',required=True,help='path to a supervisor-contract/v2 JSON file')
    args=parser.parse_args()
    try:
        raw=Path(args.contract).read_text(encoding='utf-8')
        return launch(raw)
    except (OSError,ValueError,TypeError,KeyError) as exc:
        print('Invalid pilot contract:',str(exc),file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
