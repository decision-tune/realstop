from __future__ import annotations

"""Session-scoped pilot hook. Never installed as a global hook."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from . import adapters, capture, local_observations
from .contracts import parse_contract
from .ledger import Ledger
from .pilot import validate_contract, _write_private
from .policy import Acceptance, Obligation, Receipt, NextAction, Permission, Snapshot
from .coordinator import coordinate

MAX_INPUT=1024*1024


def _gap(db, state, reason):
    capture.record_gap(db,'claude',state['session'],state['task'],reason)


def _target_ok(state, payload):
    if payload.get('tool_name') != 'Write' or not isinstance(payload.get('tool_input'),dict): return False
    tool=payload['tool_input']
    if set(tool) != {'file_path','content'} or not isinstance(tool['file_path'],str) or not isinstance(tool['content'],str): return False
    root=Path(state['workspace']); c=validate_contract(state['contract'])['criteria'][0]
    expected=root/c['artifact_relative_path']; proposed=Path(tool['file_path'])
    if not proposed.is_absolute(): proposed=root/proposed
    if str(proposed) != str(expected): return False
    rootstat=os.lstat(root)
    if not stat.S_ISDIR(rootstat.st_mode) or [rootstat.st_dev,rootstat.st_ino]!=state['root_identity']: return False
    current=root
    for part in Path(c['artifact_relative_path']).parts:
        current=current/part
        try: st=os.lstat(current)
        except FileNotFoundError:
            if current != expected: return False
            break
        if stat.S_ISLNK(st.st_mode): return False
        if current != expected and not stat.S_ISDIR(st.st_mode): return False
        if current == expected and not stat.S_ISREG(st.st_mode): return False
    return True


def _deny():
    return {'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny','permissionDecisionReason':'Pilot permits only the declared file in its fresh workspace.'}}


def _registry(state, data):
    c=data['criteria'][0]; a=data['actions'][0]
    return {'roots':{'workspace':{'path':state['workspace'],'environment_id':c['environment_id']}},
            'environments':[c['environment_id']],
            'operations':{a['operation_id']:{'artifact_relative_path':c['artifact_relative_path'],'check':'sha256','environment_id':c['environment_id']}}}


def _stop(db,state,private,payload):
    # Each delivery is saved independently; this name is not a native event identity.
    _write_private(private/('stop-'+uuid.uuid4().hex+'.json'),payload)
    if payload.get('stop_hook_active') is not False: return {}
    if payload.get('permission_mode') == 'plan' or payload.get('background_tasks') or payload.get('session_crons'): return {}
    final=payload.get('last_assistant_message')
    if not isinstance(final,str) or len(final)>100000: return {}
    data=validate_contract(state['contract']); registry=_registry(state,data)
    scope=db.read_scope('claude',state['session'],state['task'])
    if scope['gaps'] or any(e['kind']=='lifecycle' for e in scope['events']): return {}
    declarations=parse_contract(db,'claude',state['session'],state['task'],fresh_owned_scope=True,
                                operations=registry['operations'],environments=set(registry['environments']))
    c=data['criteria'][0]; a=data['actions'][0]
    oid=local_observations.check_and_record(db,'claude',state['session'],state['task'],c['id'],
        fresh_owned_scope=True,registry=registry,root_id='workspace',expected_version=declarations.ledger_version)
    scope=db.read_scope('claude',state['session'],state['task'])
    row=next(row for row in scope['observations'] if local_observations.PREFIX+row['observation_id']==oid)
    observation=local_observations.validate_record(row,registry,'claude',state['session'],state['task'])['observation']
    result={'match':'pass','mismatch':'fail','missing':'fail','unverifiable':'unknown'}[observation['status']]
    if result != 'fail': return {}
    # Consumed before the verifier starts; a crash cannot cause a second attempt.
    try:
        fd=os.open(str(private/'attempt-reserved'),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError: return {}
    os.close(fd)
    acceptance=Acceptance(c['artifact_relative_path'],c['expected_sha256'],'sha256',c['environment_id'])
    snap=Snapshot(state['task'],str(scope['version']),hashlib.sha256(final.encode()).hexdigest(),
        'single-pilot-attempt',obligations=(Obligation(c['id'],'owner',(acceptance,)),),
        receipts=(Receipt(c['id'],acceptance,result,oid),),
        actions=(NextAction(a['id'],c['id'],acceptance,'owner'),),
        permissions=(Permission(a['id'],'allowed','owner'),),coverage='supported',feasibility='actionable',
        authority_complete=True,scope_resolved=True)
    proposed=('Use Write on '+c['artifact_relative_path']+' to satisfy the explicit contract; '
              'the current artifact check is '+observation['status']+'.')
    outcome=coordinate(db,'claude',state['session'],state['task'],snap,a['id'],proposed,final,
                       {'owner':declarations.source_event_id},timeout=30,observation_registry=registry)
    _write_private(private/'attempt-result.json',{'reason':outcome.get('reason'),'claim_saved':outcome.get('claim_saved') is True})
    if outcome.get('claim_saved') is True:
        return {'decision':'block','reason':proposed}
    return {}


def handle(private,payload):
    state=json.loads((private/'state.json').read_text(encoding='utf-8'))
    if not isinstance(payload,dict) or payload.get('session_id') != state['session']:
        return _deny() if isinstance(payload,dict) and payload.get('hook_event_name')=='PreToolUse' else {}
    name=payload.get('hook_event_name')
    db=Ledger(private/'ledger.sqlite3')
    try:
        if payload.get('agent_id') is not None or payload.get('agent_type') is not None:
            _gap(db,state,'pilot_subagent'); return _deny() if name=='PreToolUse' else {}
        if name in ('PreCompact','PostCompact','InstructionsLoaded','SubagentStart','SubagentStop'):
            _gap(db,state,'pilot_unsupported_lifecycle'); return {}
        if name=='Stop': return _stop(db,state,private,payload)
        if name=='UserPromptSubmit':
            try:
                fd=os.open(str(private/'user-seen'),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); os.close(fd)
            except FileExistsError:
                _gap(db,state,'pilot_extra_user'); return {}
            if payload.get('prompt') != state['contract']:
                _gap(db,state,'pilot_unexpected_user'); return {}
        if name=='PreToolUse' and not _target_ok(state,payload):
            _gap(db,state,'pilot_disallowed_tool'); return _deny()
        if name in ('UserPromptSubmit','PreToolUse','PostToolUse','PostToolUseFailure'):
            try: capture.capture(db,'claude',state['task'],state['session'],payload)
            except Exception:
                _gap(db,state,'pilot_capture_error')
                return _deny() if name=='PreToolUse' else {}
            if name in ('PostToolUse','PostToolUseFailure') and not _target_ok(state,payload):
                _gap(db,state,'pilot_unexpected_tool_result')
        else: _gap(db,state,'pilot_unknown_event')
        return {}
    finally: db.close()


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--state',required=True); args=parser.parse_args()
    payload=None
    try:
        raw=sys.stdin.buffer.read(MAX_INPUT+1)
        if len(raw)>MAX_INPUT: raise ValueError('oversized input')
        payload=json.loads(raw)
        response=handle(Path(args.state),payload)
    except Exception:
        # Best effort persistent gap: a failure must not later look complete.
        try:
            private=Path(args.state); state=json.loads((private/'state.json').read_text()); db=Ledger(private/'ledger.sqlite3')
            try: _gap(db,state,'pilot_hook_error')
            finally: db.close()
        except Exception: pass
        response=_deny() if isinstance(payload,dict) and payload.get('hook_event_name')=='PreToolUse' else {}
    print(json.dumps(response))


if __name__=='__main__': main()
