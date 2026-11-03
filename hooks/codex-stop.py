"""Codex CLI main-agent hooks. Keep only task fingerprints, tool counts and decisions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OFFER = re.compile(r"(?:should I|want me to|would you like me to|do you want me to)\s+(?:also\s+)?(.+?)\s*(?:as well)?\s*\?\s*$", re.I)
ACTION = re.compile(r"(?:add|write|update|create|edit|fix|implement|document|test)\s+[\w./ -]{1,180}", re.I)
CAUTION = re.compile(r"\b(?:if|unless|only|not|avoid|except|before|after|approval|permission|credential|denied|blocked|wait(?:ing)?|background|don't|do not|never|instead|optional|maybe|consider|example|either|or|stop|cancel|skip|without)\b", re.I)
NUDGE = ("Review the original user request. If the offered step is explicitly requested, still unfinished, "
         "and can be completed with your current permissions, finish it and verify it. If already complete, "
         "optional, blocked, or awaiting approval, stop. Keep all existing permission boundaries.")


def fingerprint(task):
    words = re.sub(r"\b(?:a|an|the|requested)\b", " ", task.lower())
    return hashlib.sha256(" ".join(words.split()).encode()).hexdigest()


def requested(prompt, rules):
    # Accept direct, unconditional imperatives only. Hashes avoid storing prompt text.
    if any(mark in prompt for mark in ('"', "“", "”", "`", "?")) or CAUTION.search(prompt) or rules["USER_STOP"].search(prompt) or rules["RISKY"].search(prompt):
        return []
    tasks = []
    for clause in re.split(r"[.!](?:\s+|$)|[;,\n]\s*|\s+and\s+", prompt):
        clause = re.sub(r"^please\s+", "", clause.strip(), flags=re.I)
        if ACTION.fullmatch(clause):
            tasks.append(fingerprint(clause))
    return tasks


def handle(db, event, mode, rules):
    sid, tid = event.get("session_id"), event.get("turn_id")
    if not sid or not tid or event.get("agent_id") or event.get("permission_mode") == "plan":
        return
    key = hashlib.sha256(json.dumps([sid, tid]).encode()).hexdigest()
    name = event.get("hook_event_name")
    now = int(time.time())
    db.execute("CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, tasks TEXT, tools INTEGER DEFAULT 0, unsafe INTEGER DEFAULT 0, seen INTEGER DEFAULT 0, created INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS totals (result TEXT PRIMARY KEY, n INTEGER)")
    db.execute("BEGIN IMMEDIATE")
    if name == "UserPromptSubmit":
        db.execute("DELETE FROM turns WHERE created < ?", (now - 86400,))
        tasks = requested(event.get("prompt") or "", rules)
        # A repeated submit in the same turn may be an automatic continuation.
        db.execute("INSERT INTO turns(id,tasks,created) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET tasks='[]'",
                   (key, json.dumps(tasks), now))
        return
    if name == "PostToolUse":
        response = event.get("tool_response")
        # Unknown and failed outcomes can never supply permission to continue.
        output = json.dumps(response, ensure_ascii=False) if response is not None else ""
        failed = isinstance(response, dict) and response.get("exit_code", 0) not in (0, None)
        unsafe = not output or failed or bool(re.search(r'(?i)denied|refused|permission|background|process running|session id|exited with code [1-9]|"is_?error"\s*:\s*true', output))
        db.execute("UPDATE turns SET tools=tools+1, unsafe=max(unsafe,?) WHERE id=?", (int(unsafe), key))
        return
    if name != "Stop":
        return
    row = db.execute("SELECT tasks,tools,unsafe,seen FROM turns WHERE id=?", (key,)).fetchone()
    if not row or row[3] or event.get("stop_hook_active") is not False:
        return
    db.execute("UPDATE turns SET seen=1 WHERE id=?", (key,))
    reply = event.get("last_assistant_message") or ""
    body = rules["strip_quoted"](rules["without_question_examples"](reply))
    match = OFFER.search(body)
    hits = rules["quit_hits"](body, row[1], [])
    eligible = (hits and match and not row[2] and not CAUTION.search(body) and not rules["RISKY"].search(body)
                and not rules["GATE_TOKEN"].search(reply) and ACTION.fullmatch(match[1].strip())
                and not rules["ACTION"].search(body) and fingerprint(match[1]) in json.loads(row[0]))
    result = ("block" if mode == "block" else "would_block") if eligible else ("other_rule_hit" if hits else "pass")
    db.execute("INSERT INTO totals VALUES(?,1) ON CONFLICT(result) DO UPDATE SET n=n+1", (result,))
    if result == "block":
        return {"decision": "block", "reason": NUDGE}


def main():
    os.umask(0o077)
    try:
        conf = json.loads((ROOT / "codex.json").read_text())
        if conf.get("paused", True) or conf.get("mode") not in ("shadow", "block"):
            return
        event = json.load(sys.stdin)
        rules = runpy.run_path(str(ROOT / "no-quit.py"))
        with sqlite3.connect(ROOT / "events.sqlite3", timeout=1) as db:
            decision = handle(db, event, conf["mode"], rules)
        if decision:
            print(json.dumps(decision))
    except Exception as exc:
        print("RealStop Codex hook passed after " + type(exc).__name__, file=sys.stderr)


if __name__ == "__main__":
    main()
