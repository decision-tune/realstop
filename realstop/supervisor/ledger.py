from __future__ import annotations

"""Local event ledger; no interpretation or continuation policy."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote


class Conflict(ValueError):
    """An identity was reused for different content."""


def _id(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError('identity must be a nonempty string of at most 512 characters')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Ledger:
    def __init__(self, path, *, readonly=False):
        self.path = Path(path).expanduser().absolute()
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.parent.stat().st_mode & 0o077:
                raise ValueError('ledger directory must be private')
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try: os.fchmod(fd, 0o600)
            finally: os.close(fd)
            self.db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                  seq INTEGER PRIMARY KEY, provider TEXT NOT NULL, session TEXT NOT NULL,
                  task TEXT NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL,
                  call_id TEXT, original TEXT NOT NULL, digest TEXT NOT NULL,
                  UNIQUE(provider, event_id));
                CREATE INDEX IF NOT EXISTS events_scope ON events(provider,session,task);
                CREATE TABLE IF NOT EXISTS gaps (
                  seq INTEGER PRIMARY KEY, provider TEXT NOT NULL, session TEXT NOT NULL,
                  task TEXT NOT NULL, gap_id TEXT NOT NULL, reason TEXT NOT NULL,
                  UNIQUE(provider, session, task, gap_id));
                CREATE TABLE IF NOT EXISTS snapshots (
                  provider TEXT NOT NULL, session TEXT NOT NULL, task TEXT NOT NULL,
                  version INTEGER NOT NULL, PRIMARY KEY(provider, session, task));
                CREATE TABLE IF NOT EXISTS local_observations (
                  seq INTEGER PRIMARY KEY, provider TEXT NOT NULL, session TEXT NOT NULL,
                  task TEXT NOT NULL, observation_id TEXT NOT NULL UNIQUE,
                  root_path TEXT NOT NULL, original TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS local_observations_scope ON local_observations(provider,session,task);
                CREATE TABLE IF NOT EXISTS intervention_claims (
                  seq INTEGER PRIMARY KEY, provider TEXT NOT NULL, session TEXT NOT NULL,
                  task TEXT NOT NULL, obligation TEXT NOT NULL, evidence_digest TEXT NOT NULL,
                  progress_id TEXT NOT NULL, decision_digest TEXT NOT NULL, version INTEGER NOT NULL);
            """)
        else:
            self.db = sqlite3.connect('file:' + quote(str(self.path)) + '?mode=ro', uri=True)
        self.db.row_factory = sqlite3.Row

    def close(self): self.db.close()

    def _write(self, table, key, content, insert, params, scope):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT * FROM ' + table + ' WHERE ' + key[0], key[1]).fetchone()
            if row is not None:
                if any(row[k] != v for k, v in content.items()): raise Conflict('identity reused with different content')
                self.db.execute('COMMIT'); return False
            self.db.execute(insert, params)
            self.db.execute('INSERT INTO snapshots VALUES(?,?,?,1) ON CONFLICT(provider,session,task) DO UPDATE SET version=version+1', scope)
            self.db.execute('COMMIT'); return True
        except BaseException:
            self.db.execute('ROLLBACK'); raise

    def append(self, event):
        if not isinstance(event, dict) or set(event) != {'provider','session','task','event_id','kind','payload','call_id'}:
            raise ValueError('invalid event fields')
        provider, session, task, eid = (_id(event[x]) for x in ('provider','session','task','event_id'))
        kind = _id(event['kind'])
        if kind not in ('user','assistant','tool_call','tool_result','lifecycle'):
            raise ValueError('unsupported event kind')
        call = event['call_id']
        if kind in ('tool_call','tool_result'): _id(call)
        elif call is not None: raise ValueError('call id only valid for tool events')
        original = _json(event['payload'])
        content = dict(session=session, task=task, kind=kind, call_id=call, original=original)
        return self._write('events', ('provider=? AND event_id=?',(provider,eid)), content,
            'INSERT INTO events(provider,session,task,event_id,kind,call_id,original,digest) VALUES(?,?,?,?,?,?,?,?)',
            (provider,session,task,eid,kind,call,original,hashlib.sha256(original.encode()).hexdigest()), (provider,session,task))

    def gap(self, provider, session, task, gap_id, reason):
        provider, session, task, gap_id, reason = map(_id,(provider,session,task,gap_id,reason))
        return self._write('gaps', ('provider=? AND session=? AND task=? AND gap_id=?',(provider,session,task,gap_id)), {'reason':reason},
            'INSERT INTO gaps(provider,session,task,gap_id,reason) VALUES(?,?,?,?,?)',
            (provider,session,task,gap_id,reason), (provider,session,task))

    def snapshot(self, provider, session, task):
        row=self.db.execute('SELECT version FROM snapshots WHERE provider=? AND session=? AND task=?',(provider,session,task)).fetchone()
        return row[0] if row else 0

    def is_current(self, provider, session, task, version):
        return self.snapshot(provider,session,task) == version

    def claim_intervention(self, provider, session, task, *, expected_version,
                           obligation, evidence_digest, progress_id, decision_digest):
        """Persist a caller-validated claim at the current local event version.

        This is not permission to act or evidence of native confirmation. The
        caller must validate those before calling, and recheck external artifacts
        and host permissions. SQLite cannot atomically lock the external host.
        """
        scope = tuple(map(_id, (provider, session, task)))
        obligation, evidence_digest, progress_id, decision_digest = map(
            _id, (obligation, evidence_digest, progress_id, decision_digest))
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError('invalid expected version')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.snapshot(*scope) != expected_version:
                self.db.execute('COMMIT'); return False
            previous = self.db.execute('''SELECT 1 FROM intervention_claims
                WHERE provider=? AND session=? AND task=? AND obligation=?
                AND (evidence_digest=? OR progress_id=?) LIMIT 1''',
                (*scope, obligation, evidence_digest, progress_id)).fetchone()
            if previous is not None:
                self.db.execute('COMMIT'); return False
            self.db.execute('''INSERT INTO intervention_claims
                (provider,session,task,obligation,evidence_digest,progress_id,decision_digest,version)
                VALUES(?,?,?,?,?,?,?,?)''',
                (*scope, obligation, evidence_digest, progress_id, decision_digest, expected_version))
            self.db.execute('COMMIT'); return True
        except BaseException:
            self.db.execute('ROLLBACK'); raise

    def intervention_claims(self, provider, session, task):
        return [dict(row) for row in self.db.execute('''SELECT obligation,
            evidence_digest,progress_id,decision_digest,version FROM intervention_claims
            WHERE provider=? AND session=? AND task=? ORDER BY seq''',
            (provider,session,task))]

    def _record_local_observation(self, provider, session, task, *, expected_version, observation_id, root_path, original):
        """Internal trusted check-and-record sink, not a native hook ingest API."""
        scope=tuple(map(_id,(provider,session,task))); _id(observation_id)
        if type(expected_version) is not int or expected_version < 0: raise ValueError('invalid_version')
        digest=hashlib.sha256(original.encode('utf-8')).hexdigest()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.snapshot(*scope)!=expected_version: raise Conflict('stale_version')
            row=self.db.execute('SELECT * FROM local_observations WHERE observation_id=?',(observation_id,)).fetchone()
            if row:
                if (row['provider'],row['session'],row['task'],row['root_path'],row['original'],row['digest']) != (*scope,root_path,original,digest): raise Conflict('observation_identity_conflict')
                self.db.execute('COMMIT'); return False
            self.db.execute('INSERT INTO local_observations(provider,session,task,observation_id,root_path,original,digest) VALUES(?,?,?,?,?,?,?)',(*scope,observation_id,root_path,original,digest))
            self.db.execute('INSERT INTO snapshots VALUES(?,?,?,1) ON CONFLICT(provider,session,task) DO UPDATE SET version=version+1',scope)
            self.db.execute('COMMIT'); return True
        except BaseException:
            self.db.execute('ROLLBACK'); raise

    def read_scope(self, provider, session, task, *, max_events=256, max_bytes=32000):
        """Consistent bounded snapshot of scoped events, gaps and persisted claims."""
        scope = tuple(map(_id, (provider, session, task)))
        self.db.execute('BEGIN')
        try:
            version = self.snapshot(*scope)
            count, size = self.db.execute(
                'SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(original AS BLOB))),0) FROM events WHERE provider=? AND session=? AND task=?', scope).fetchone()
            observation_count,observation_size=self.db.execute('SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(original AS BLOB))),0) FROM local_observations WHERE provider=? AND session=? AND task=?',scope).fetchone()
            if count+observation_count > max_events or size+observation_size > max_bytes:
                raise ValueError('scope_over_budget')
            rows = self.db.execute('SELECT event_id,kind,call_id,original FROM events WHERE provider=? AND session=? AND task=? ORDER BY seq', scope).fetchall()
            events = [{'event_id':row['event_id'], 'kind':row['kind'], 'call_id':row['call_id'],
                       'payload':json.loads(row['original'])} for row in rows]
            gaps = self.db.execute('SELECT COUNT(*) FROM gaps WHERE provider=? AND session=? AND task=?',scope).fetchone()[0]
            calls = self.calls(*scope)
            claims = self.intervention_claims(*scope)
            observations=[dict(row) for row in self.db.execute('SELECT observation_id,root_path,original,digest FROM local_observations WHERE provider=? AND session=? AND task=? ORDER BY seq',scope)]
            self.db.execute('COMMIT')
            return {'version':version, 'events':events, 'gaps':gaps, 'calls':calls, 'claims':claims, 'observations':observations}
        except BaseException:
            self.db.execute('ROLLBACK'); raise

    def read_event(self, provider, event_id):
        row=self.db.execute('SELECT * FROM events WHERE provider=? AND event_id=?',(provider,event_id)).fetchone()
        if row is None: return None
        return {'provider':row['provider'], 'session':row['session'], 'task':row['task'], 'event_id':row['event_id'],
                'kind':row['kind'], 'call_id':row['call_id'], 'payload':json.loads(row['original'])}

    def calls(self, provider, session, task):
        rows=self.db.execute("SELECT call_id,kind,COUNT(*) AS n FROM events WHERE provider=? AND session=? AND task=? AND call_id IS NOT NULL GROUP BY call_id,kind",(provider,session,task))
        counts = {}
        for row in rows: counts.setdefault(row['call_id'], {})[row['kind']] = row['n']
        def state(c):
            a,b=c.get('tool_call',0),c.get('tool_result',0)
            if a > 1 or b > 1: return 'ambiguous'
            return 'terminal' if a and b else 'pending' if a else 'orphan_result'
        return {key:state(c) for key,c in counts.items()}

    def status(self, provider, session, task):
        scope=(provider,session,task)
        self.db.execute('BEGIN')
        try:
            result = {'provider':provider,'session':session,'task':task,'version':self.snapshot(*scope),
                      'events':self.db.execute('SELECT COUNT(*) FROM events WHERE provider=? AND session=? AND task=?',scope).fetchone()[0],
                      'gaps':self.db.execute('SELECT COUNT(*) FROM gaps WHERE provider=? AND session=? AND task=?',scope).fetchone()[0],
                      'calls':self.calls(*scope)}
            self.db.execute('COMMIT')
            return result
        except BaseException:
            self.db.execute('ROLLBACK'); raise



def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('status','export')); p.add_argument('database')
    p.add_argument('provider'); p.add_argument('session'); p.add_argument('task')
    a=p.parse_args(); db=Ledger(a.database,readonly=True)
    try: print(json.dumps(db.status(a.provider,a.session,a.task),sort_keys=True))
    finally: db.close()

if __name__ == '__main__': main()
