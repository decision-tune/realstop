from __future__ import annotations

"""Explicit declaration ingress for a host-attested fresh local scope."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
import uuid

from .adapters import adapt

MAX_BYTES = 8192
MAX_ITEMS = 16

class ContractError(ValueError):
    pass


def _pairs(pairs):
    out={}
    for key,value in pairs:
        if key in out: raise ContractError('duplicate_field')
        out[key]=value
    return out


def _fields(value, keys):
    if not isinstance(value,dict) or set(value)!=set(keys): raise ContractError('invalid_fields')


def _id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',value): raise ContractError('invalid_id')
    return value


def _path(value):
    if not isinstance(value,str) or not value or len(value)>512 or '\\' in value or ':' in value:
        raise ContractError('invalid_path')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ContractError('invalid_path') from exc
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ContractError('invalid_path')
    parts=value.split('/')
    if value.startswith('/') or any(x in ('','.','..') for x in parts) or str(PurePosixPath(value))!=value:
        raise ContractError('invalid_path')
    return value


def _stable(*parts):
    return hashlib.sha256(json.dumps(parts,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


@dataclass(frozen=True)
class CriterionDeclaration:
    id: str
    stable_id: str
    artifact_relative_path: str
    expected_sha256: str
    check: str
    environment_id: str


@dataclass(frozen=True)
class ActionDeclaration:
    id: str
    stable_id: str
    criterion_id: str
    operation_id: str
    permission: str


@dataclass(frozen=True)
class Declarations:
    contract_id: str
    task_id: str
    source_event_id: str
    ledger_version: int
    criteria: tuple[CriterionDeclaration,...]
    actions: tuple[ActionDeclaration,...]


def parse_contract(db, provider, session, task, *, fresh_owned_scope, operations, environments):
    """Return declarations only; fresh_owned_scope is a trusted host attestation."""
    if fresh_owned_scope is not True: raise ContractError('fresh_scope_required')
    if provider not in ('claude','codex') or not all(isinstance(v,str) and v for v in (session,task)):
        raise ContractError('invalid_scope')
    if not isinstance(operations,dict) or not isinstance(environments,(set,frozenset)):
        raise ContractError('invalid_registry')
    try: scope=db.read_scope(provider,session,task)
    except Exception as exc: raise ContractError('unavailable_scope') from exc
    if scope['gaps'] or any(v!='terminal' for v in scope['calls'].values()): raise ContractError('incomplete_scope')
    events=scope['events']
    users=[e for e in events if e['kind']=='user']
    if len(users)!=1 or not events or events[0] is not users[0]: raise ContractError('invalid_origin')
    event=users[0]; payload=event['payload']
    if (not isinstance(payload,dict) or payload.get('session_id')!=session or
        payload.get('hook_event_name')!='UserPromptSubmit' or not isinstance(payload.get('prompt'),str) or
        payload.get('agent_id') is not None or payload.get('agent_type') is not None):
        raise ContractError('invalid_origin')
    # Require identity derivable from the provider adapter; random IDs carry a gap.
    try: derived,gap=adapt(provider,task,payload)
    except Exception as exc: raise ContractError('invalid_origin') from exc
    if gap is not None or derived['event_id']!=event['event_id']: raise ContractError('invalid_origin')
    prompt=payload['prompt']
    try: prompt_bytes=prompt.encode('utf-8')
    except UnicodeEncodeError as exc: raise ContractError('invalid_json') from exc
    if len(prompt_bytes)>MAX_BYTES: raise ContractError('contract_too_large')
    try: data=json.loads(prompt,object_pairs_hook=_pairs,parse_constant=lambda _: (_ for _ in ()).throw(ContractError('invalid_json')))
    except (ValueError,TypeError,RecursionError) as exc: raise ContractError('invalid_json') from exc
    if isinstance(data, dict) and data.get('schema') == 'supervisor-contract/v2':
        _fields(data,('schema','contract_id','revision','task_id','task_instruction','criteria','actions'))
        if (not isinstance(data['task_instruction'], str) or not data['task_instruction'].strip()
                or len(data['task_instruction']) > 4096):
            raise ContractError('invalid_instruction')
        revision = 2
    else:
        _fields(data,('schema','contract_id','revision','task_id','criteria','actions'))
        revision = 1
    if data['schema'] != f'supervisor-contract/v{revision}' or type(data['revision']) is not int or data['revision'] != revision or data['task_id']!=task:
        raise ContractError('invalid_contract')
    try:
        contract_id=str(uuid.UUID(data['contract_id']))
        if contract_id != data['contract_id']: raise ValueError()
    except (ValueError,TypeError,AttributeError) as exc: raise ContractError('invalid_contract_id') from exc
    criteria, actions=data['criteria'],data['actions']
    if not isinstance(criteria,list) or not 1<=len(criteria)<=MAX_ITEMS or not isinstance(actions,list) or not 1<=len(actions)<=MAX_ITEMS:
        raise ContractError('invalid_count')
    if revision == 2 and (len(criteria) != 1 or len(actions) != 1): raise ContractError('invalid_count')
    parsed=[]; by_id={}
    for c in criteria:
        _fields(c,('id','artifact_relative_path','expected_sha256','check','environment_id'))
        cid=_id(c['id']); artifact=_path(c['artifact_relative_path']); environment=_id(c['environment_id'])
        if cid in by_id or c['check']!='sha256' or environment not in environments or not isinstance(c['expected_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',c['expected_sha256']):
            raise ContractError('invalid_criterion')
        value=CriterionDeclaration(cid,_stable(provider,session,task,event['event_id'],'criterion',cid),artifact,c['expected_sha256'],'sha256',environment)
        parsed.append(value); by_id[cid]=value
    parsed_actions=[]; ids=set(); operation_ids=set()
    for a in actions:
        _fields(a,('id','criterion_id','operation_id','permission'))
        aid=_id(a['id']); cid=_id(a['criterion_id']); op=_id(a['operation_id'])
        if aid in ids or op in operation_ids or cid not in by_id or a['permission'] not in ('allowed','denied') or op not in operations:
            raise ContractError('invalid_action')
        c=by_id[cid]; rule=operations[op]
        if not isinstance(rule,dict) or rule != {'artifact_relative_path':c.artifact_relative_path,'check':c.check,'environment_id':c.environment_id}:
            raise ContractError('operation_scope_mismatch')
        ids.add(aid); operation_ids.add(op)
        parsed_actions.append(ActionDeclaration(aid,_stable(provider,session,task,event['event_id'],'action',aid),cid,op,a['permission']))
    if db.snapshot(provider,session,task) != scope['version']: raise ContractError('stale_scope')
    return Declarations(contract_id,task,event['event_id'],scope['version'],tuple(parsed),tuple(parsed_actions))
