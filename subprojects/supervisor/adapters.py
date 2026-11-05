"""Versioned, conservative adapters for documented command-hook inputs."""
import hashlib
import json
import uuid

SCHEMA = 'native-hooks-v1'
SUPPORTED = {
    'claude': {'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'Stop', 'SessionStart', 'SessionEnd', 'PreCompact', 'PostCompact'},
    'codex': {'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'SessionStart', 'SessionEnd', 'PreCompact', 'PostCompact'},
}

class Rejected(ValueError):
    def __init__(self, reason): self.reason = reason


def _valid_id(value):
    return isinstance(value, str) and 0 < len(value) <= 512


def _scoped_id(*parts):
    raw = json.dumps(parts, separators=(',', ':'), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def adapt(provider, task, payload):
    if provider not in SUPPORTED or not isinstance(payload, dict): raise Rejected('invalid_payload')
    sid, name = payload.get('session_id'), payload.get('hook_event_name')
    if not _valid_id(sid) or not _valid_id(task): raise Rejected('missing_scope')
    if name not in SUPPORTED[provider]: raise Rejected('unsupported_event')
    # Subagents share host session IDs in some providers. Never promote them.
    if payload.get('agent_id') is not None or payload.get('agent_type') is not None:
        raise Rejected('subagent_scope_unsupported')
    if name == 'UserPromptSubmit':
        if not isinstance(payload.get('prompt'), str): raise Rejected('missing_prompt')
        kind = 'user'
    elif name == 'PreToolUse': kind = 'tool_call'
    elif name in ('PostToolUse', 'PostToolUseFailure'): kind = 'tool_result'
    elif name == 'Stop':
        if not isinstance(payload.get('last_assistant_message'), str): raise Rejected('missing_final_reply')
        kind = 'assistant'
    else: kind = 'lifecycle'
    call = None
    if kind in ('tool_call', 'tool_result'):
        call = payload.get('tool_use_id')
        if not _valid_id(call): raise Rejected('missing_tool_id')
        if not isinstance(payload.get('tool_name'), str) or 'tool_input' not in payload: raise Rejected('invalid_tool_input')
        if name == 'PostToolUse' and 'tool_response' not in payload: raise Rejected('missing_tool_response')
        if name == 'PostToolUseFailure' and not isinstance(payload.get('error'), str): raise Rejected('missing_tool_error')
        # One pre and one terminal delivery per tool ID. Changed duplicates conflict.
        event_id = _scoped_id(SCHEMA, provider, sid, 'call' if kind == 'tool_call' else 'result', call)
        gap = None
    elif provider == 'claude' and name == 'UserPromptSubmit' and payload.get('prompt_id') is not None:
        try:
            prompt_id = str(uuid.UUID(payload['prompt_id']))
        except (ValueError, TypeError, AttributeError):
            raise Rejected('invalid_prompt_id')
        event_id = _scoped_id(SCHEMA, provider, sid, 'prompt', prompt_id)
        gap = None
    else:
        # No reliable delivery ID for this event; never collapse equal prompts.
        event_id = _scoped_id(SCHEMA, provider, sid, uuid.uuid4().hex)
        gap = {'Stop':'stop_invocation_identity_unavailable',
               'SessionEnd':'session_end_invocation_identity_unavailable'}.get(name, 'event_identity_unavailable')
    return {'provider':provider, 'session':sid, 'task':task, 'event_id':event_id,
            'kind':kind, 'call_id':call, 'payload':payload}, gap
