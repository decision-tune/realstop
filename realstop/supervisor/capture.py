from __future__ import annotations

"""Fail-open native hook capture; intentionally returns no intervention."""
import argparse
import json
import sys
import uuid
from .adapters import adapt, Rejected
from .ledger import Ledger

MAX_BYTES = 1024 * 1024


def record_gap(db, provider, session, task, reason):
    if session:
        db.gap(provider, session, task, uuid.uuid4().hex, reason)


def capture(db, provider, task, target, payload):
    if not isinstance(payload, dict): raise Rejected('invalid_payload')
    sid = payload.get('session_id')
    if target and sid != target: return 'filtered'
    event, gap = adapt(provider, task, payload)
    # Mark uncertainty before writing: a crash cannot hide the identity gap.
    if gap: record_gap(db, provider, sid, task, gap)
    db.append(event)
    return 'captured'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider', required=True, choices=('claude','codex'))
    parser.add_argument('--task', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--session', required=True, help='only this session is captured')
    args = parser.parse_args()
    db = None
    try:
        db = Ledger(args.database)
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES: raise Rejected('oversized_input')
        try: payload = json.loads(raw)
        except (ValueError, UnicodeError): raise Rejected('malformed_json')
        try: capture(db, args.provider, args.task, args.session, payload)
        except Rejected as exc:
            if isinstance(payload, dict) and payload.get('session_id') == args.session:
                record_gap(db, args.provider, args.session, args.task, exc.reason)
    except Rejected as exc:
        if db is not None:
            try: record_gap(db, args.provider, args.session, args.task, exc.reason)
            except Exception: pass
    except Exception:
        # No arbitrary input or exception strings may appear in hook output.
        if db is not None:
            try: record_gap(db, args.provider, args.session, args.task, 'capture_error')
            except Exception: pass
    finally:
        if db is not None:
            try: db.close()
            except Exception: pass
        print('{}')

if __name__ == '__main__': main()
