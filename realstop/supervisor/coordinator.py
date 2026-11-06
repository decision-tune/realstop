from __future__ import annotations

"""Conservative advisory coordinator over a local capture ledger."""
from dataclasses import replace
import json

from . import native_review
from . import local_observations
from .policy import Snapshot, Intervention, evaluate_policy, validate_confirmed_action


def _source(event, provider, session):
    payload=event['payload']
    if not isinstance(payload,dict) or payload.get('session_id') != session:
        raise ValueError('invalid_event')
    if payload.get('agent_id') is not None or payload.get('agent_type') is not None:
        raise ValueError('unsupported_subagent')
    name,kind=payload.get('hook_event_name'),event['kind']
    if kind == 'user' and name == 'UserPromptSubmit' and isinstance(payload.get('prompt'),str):
        return {'role':'user','text':payload['prompt']}
    if kind == 'assistant' and name == 'Stop' and isinstance(payload.get('last_assistant_message'),str):
        return {'role':'assistant','text':payload['last_assistant_message']}
    if ((kind == 'tool_call' and name == 'PreToolUse') or
        (kind == 'tool_result' and name in ('PostToolUse','PostToolUseFailure'))):
        if not event['call_id'] or payload.get('tool_use_id') != event['call_id']:
            raise ValueError('invalid_tool_event')
        return {'role':'tool','text':json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',', ':'))}
    raise ValueError('unsupported_source')


def coordinate(db, provider, session, task, snapshot, action_id, proposed_action,
               final_reply, source_events, timeout=30, *, observation_registry=None):
    """Review only a complete scoped local snapshot; returns advisory result, no action."""
    def out(reason, metadata=None, saved=False):
        return {'reason':reason,'metadata':metadata,'claim_saved':saved}
    try:
        if not isinstance(snapshot,Snapshot) or snapshot.task_id != task:
            return out('invalid_snapshot')
        if provider not in ('claude','codex') or not isinstance(source_events,dict):
            return out('invalid_sources')
        try:
            scope=db.read_scope(provider,session,task)
        except ValueError as exc:
            if str(exc) == 'scope_over_budget': return out('scope_over_budget')
            raise
        if snapshot.version != str(scope['version']): return out('stale_snapshot')
        if scope['gaps'] or any(state != 'terminal' for state in scope['calls'].values()):
            return out('incomplete_capture')
        # Existing claims are policy facts, so repeats are suppressed before native review.
        history=tuple(Intervention(x['obligation'],x['evidence_digest'],x['progress_id']) for x in scope['claims'])
        current=replace(snapshot,interventions=tuple(dict.fromkeys(snapshot.interventions+history)))
        if evaluate_policy(current).disposition != 'needs_native_review': return out('policy_ineligible')
        events={e['event_id']:e for e in scope['events']}
        if (not all(isinstance(k,str) and k and not k.startswith(local_observations.PREFIX) and isinstance(v,str) and v for k,v in source_events.items())
            or any(eid not in events for eid in source_events.values())): return out('invalid_sources')
        # Every captured user submission must be visible in the native packet.
        users={e['event_id'] for e in scope['events'] if e['kind']=='user'}
        if not users or not users.issubset(set(source_events.values())): return out('incomplete_authority')
        sources={sid:_source(events[eid],provider,session) for sid,eid in source_events.items()}
        # Include every supported conversation/tool original, even when the
        # application omitted it from its policy facts. Never overwrite aliases.
        represented=set(source_events.values())
        for event in scope['events']:
            if event['event_id'] in represented or event['kind'] not in ('tool_call','tool_result','assistant'):
                continue
            sid='event:'+event['event_id']
            while sid in sources: sid='_'+sid
            if len(sid)>256: return out('invalid_sources')
            sources[sid]=_source(event,provider,session)
        # Local checks have a reserved namespace and are never native/user events.
        observations={}
        for row in scope['observations']:
            record=local_observations.validate_record(row,observation_registry,provider,session,task)
            sid=local_observations.PREFIX+row['observation_id']
            if sid in sources: return out('invalid_sources')
            observations[sid]=record
            sources[sid]={'role':'tool','text':row['original']}
        for receipt in current.receipts:
            if receipt.source_id not in observations: continue
            obs=observations[receipt.source_id]['observation']; criterion=obs['criterion']
            expected={'match':'pass','mismatch':'fail','missing':'fail','unverifiable':'unknown'}[obs['status']]
            if (receipt.result != expected or receipt.acceptance.artifact != criterion['artifact_relative_path'] or
                receipt.acceptance.revision != criterion['expected_sha256'] or receipt.acceptance.check != criterion['check'] or
                receipt.acceptance.environment != criterion['environment_id']):
                return out('invalid_observation_binding')
        required={f.source_id for group in (current.obligations,current.receipts,current.actions,current.permissions) for f in group}
        if not required.issubset(sources): return out('incomplete_sources')
        if any(sources[f.source_id]['role'] != 'user' for group in (current.obligations,current.permissions) for f in group):
            return out('invalid_authority_source')
        selected=next((a for a in current.actions if a.id==action_id),None)
        if selected is None: return out('policy_ineligible')
        obligation=next((o for o in current.obligations if o.id==selected.obligation_id),None)
        permissions=[p for p in current.permissions if p.action_id==action_id and p.state=='allowed']
        if obligation is None or not permissions: return out('policy_ineligible')
        refs=tuple(dict.fromkeys([obligation.source_id,selected.source_id,permissions[0].source_id]))
        if validate_confirmed_action(current,confirmed_snapshot=current.version,
                confirmed_reply_digest=current.reply_digest,action_id=action_id,source_ids=refs,native_confirmed=True) is None:
            return out('policy_ineligible')
        review=native_review.review(current,action_id,proposed_action,final_reply,sources,provider,timeout)
        claim=review.get('claim')
        if claim is None: return out(review.get('reason','review_failed'))
        metadata=review.get('metadata')
        if not isinstance(metadata,dict) or metadata.get('action_id') != action_id or not isinstance(metadata.get('binding'),str):
            return out('invalid_review')
        # Registry/root changes during native review invalidate the local evidence.
        for row in scope['observations']:
            if not local_observations.recheck_record(row,observation_registry,provider,session,task):
                return out('stale_observation')
        saved=db.claim_intervention(provider,session,task,expected_version=scope['version'],
            obligation=claim.obligation_id,evidence_digest=claim.evidence_digest,
            progress_id=claim.progress_id,decision_digest=metadata['binding'])
        return out('claim_saved' if saved else 'stale_or_duplicate',metadata if saved else None,saved)
    except Exception:
        return out('unavailable_or_invalid')
