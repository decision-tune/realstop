#!/usr/bin/env python3
"""UserPromptSubmit hook: notice when the owner corrects the agent, act on it now, and record it so learn.py improves no-quit.

A correction ("no", "that's wrong", "do this yourself", "why didn't you", "stop asking") is logged with the reply it
corrects (local only, redacted) and injects context telling me to own the miss in one line and do the work this turn.
learn.py turns the log into lower thresholds and rejected-quit examples for the Stop hook. The other direction: a live
stop or "that's fine / you were done" right after a block logs a false_block record (no context injected), which
learn.py reads to raise the block threshold. Fails open. Python 3.9.
Ceiling: phrase detection; a correction worded another way is missed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

HOME = os.path.expanduser("~/.claude/realstop")
CORRECTION = re.compile(
    r"(^\s*no\b|\bthat'?s (wrong|not (right|it|what i))|\bwrong\b|\bdo (it|this|that) yourself\b"
    r"|\byou can do (it|this|that)\b|\bwhy (didn'?t|did not|can'?t|won'?t|not) (you|u)\b"
    r"|\bstop (asking|ignoring|doing this|wasting)\b|\byou (ignored|missed|didn'?t address)\b"
    r"|\bnot what i (asked|said|meant)\b|\bi (told|asked) you\b|\bovercomplicating\b|\bdon'?t ask\b"
    # how the owner teaches after a miss (2026-10-30 replay: these were only caught by a quoted "wrong")
    r"|\b(doesn'?t|doens'?t|does not|won'?t|never) happen again\b|\bwhy (did|would) you (even )?(suggest|do|say)"
    r"|\byou should remember\b|\b(don'?t|never) (ever )?make this mistake\b|\bleave it to fate\b)",
    re.I,
)
STOP_ONLY = re.compile(r"^\s*(no,? )?(stop|wait|pause|hold on)\b(?!\s+(asking|ignoring|doing this|wasting))", re.I)  # a live stop is obeyed, not "worked around"
# a bare stop, or the prompt opening with "that's fine"; "stop asking ..." is a correction (checked first, below)
FALSE_BLOCK = re.compile(r"(^\s*(no,? )?stop[.!,]*\s*$|^\s*that'?s fine\b|\byou were done\b|\bit was done\b|\bleave it\b)", re.I)
BLOCK_WINDOW_S = 15 * 60


BYPASS_OFF = re.compile(r"^\s*realstop (off|pause)\s*[.!]?\s*$", re.I)
BYPASS_ON = re.compile(r"^\s*realstop (on|resume)\s*[.!]?\s*$", re.I)
BYPASS_ONCE = re.compile(r"^\s*let it stop\s*[.!]?\s*$", re.I)


def control_command(prompt: str, session: str) -> str:
    """The owner's one-word bypass: "realstop off" pauses RealStop until "realstop on"; "let it stop" lets this
    session's next stop through once. Matches the whole prompt only. Writes control.json (read by no-quit.py)."""
    if BYPASS_OFF.match(prompt):
        what = "paused"
    elif BYPASS_ON.match(prompt):
        what = "resumed"
    elif BYPASS_ONCE.match(prompt) and session:
        what = "once"
    else:
        return ""
    path = os.path.join(HOME, "control.json")
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        if not isinstance(c, dict):
            c = {}
    except (OSError, ValueError):
        c = {}
    if what == "once":
        once = c.get("once") if isinstance(c.get("once"), dict) else {}
        once[session] = time.time()
        c["once"] = {k: v for k, v in once.items() if time.time() - float(v) < 3600}
    else:
        c["paused"] = what == "paused"
    os.makedirs(HOME, exist_ok=True)
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f)
    os.replace(tmp, path)
    return what


def claimed(session: str, prompt: str) -> bool:
    """True when another registration of this hook (plugin and install.sh entries, either install order) already
    handled this exact prompt in the last 30 s: the first run creates a lock file (O_EXCL), the second backs off.
    Fails open (not claimed)."""
    try:
        d = os.path.join(HOME, "state")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "prompt-%s.lock" % hashlib.sha1((session + "|" + prompt).encode()).hexdigest()[:16])
        for _ in range(2):
            try:
                os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return False
            except FileExistsError:
                if time.time() - os.path.getmtime(path) < 30:
                    return True
                os.unlink(path)
    except Exception:
        pass
    return False


def err(e) -> None:
    """A swallowed exception leaves its type in errors.jsonl (never prompt or reply text), so a broken hook shows."""
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(os.path.join(HOME, "errors.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": int(time.time()), "hook": "correction", "where": "main", "type": type(e).__name__}) + "\n")
    except OSError:
        pass


def redact(s: str) -> str:
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", s)
    s = re.sub(r"/(Users|home)/[^/\s]+", "~", s)
    s = re.sub(r"\b[A-Za-z0-9_\-]{32,}\b", "[redacted]", s)
    return re.sub(r"\+?\d[\d\s().-]{8,}\d", "[number]", s)


def last_reply(path: str) -> str:
    texts = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                c = d.get("message", {}).get("content")
                if d.get("type") == "user" and not d.get("toolUseResult") and not d.get("isMeta"):
                    if isinstance(c, str) or any(isinstance(x, dict) and x.get("type") == "text" for x in c or []):
                        texts = []
                elif d.get("type") == "assistant":
                    texts += [x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text"]
    except OSError:
        pass
    return "\n".join(texts)


def blocked_tail(session: str) -> str | None:
    """The tail of this session's latest stop if it was a block within the last 15 minutes, else None."""
    last = None
    try:
        with open(os.path.join(HOME, "stops.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("session") == session:
                    last = d
    except OSError:
        return None
    if last and last.get("block") and time.time() - last.get("at", 0) <= BLOCK_WINDOW_S:
        return str(last.get("tail", ""))
    return None


def own_words(prompt: str) -> str:
    """The owner's own words only: quoted or pasted text (often my earlier reply) is not a correction.
    Owner report, 2026-10-30: a question quoting "wrong-nudge" from my reply was logged as a correction."""
    s = re.sub(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", " ", prompt, flags=re.S)
    s = re.sub(r"[\"\u201c][^\"\u201d]{0,4000}[\"\u201d]", " ", s, flags=re.S)
    s = re.sub(r"`[^`\n]*`", " ", s)
    s = re.sub(r"^\s*(>|[*•-] ).*$", " ", s, flags=re.M)  # blockquotes and pasted bullet lists
    return re.sub(r"\b\w+-\w+\b", " ", s)  # compounds like "wrong-nudge" are terms, not verdicts


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        raw = payload.get("prompt", "")
        if "<task-notification>" in raw:  # checked on the raw text: own_words() erases hyphen compounds like this tag
            return
        prompt = own_words(raw)
        session = payload.get("session_id", "")
        if session and claimed(str(session), raw):
            return  # a second registration of this hook already handled this prompt
        what = control_command(raw, str(session))
        if what:
            note = {"paused": "RealStop is paused: stops go through until the owner types \"realstop on\".",
                    "resumed": "RealStop is back on.",
                    "once": "RealStop will let the next stop in this session through."}[what]
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                  "additionalContext": note + " Acknowledge in one short line and continue; this is not a correction."}}))
            return
        # a correction wins over a false block: "stop asking, do it yourself" confirms the block was right
        if (session and "<task-notification>" not in prompt and FALSE_BLOCK.search(prompt[:200])
                and not CORRECTION.search(re.sub(r"^\s*no\b,?", "", prompt[:600]))):
            tail = blocked_tail(session)
            if tail is not None:  # the owner pushed back on a block: log it, inject nothing
                os.makedirs(HOME, exist_ok=True)
                with open(os.path.join(HOME, "corrections.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps({"at": int(time.time()), "session": session, "kind": "false_block",
                                        "said": redact(prompt)[:400], "reply_tail": tail}) + "\n")
                return
        if "<task-notification>" in prompt or STOP_ONLY.search(prompt) or not CORRECTION.search(prompt[:600]):
            return
        reply = last_reply(payload.get("transcript_path", ""))
        os.makedirs(HOME, exist_ok=True)
        with open(os.path.join(HOME, "corrections.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": int(time.time()), "session": payload.get("session_id", ""),
                "said": redact(prompt)[:400], "reply_tail": redact(reply.strip())[-240:],
            }) + "\n")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "The owner is correcting your last reply. Before anything else: name what you got wrong in one plain "
                "line (no apology paragraph), then do the work they are pointing at in this turn. Do not defend the "
                "earlier choice, re-ask, or hand it back. If it reveals a repeatable miss, fix the class "
                "(code, hook or config), not just this instance."
            ),
        }}))
    except Exception as e:
        err(e)
        return


if __name__ == "__main__":
    main()
