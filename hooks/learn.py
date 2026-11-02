#!/usr/bin/env python3
"""SessionStart housekeeping for `realstop learn`. Silent (prints nothing: SessionStart output reaches the agent), local, fast.

Learning is review-gated: nothing here changes a threshold, an example or a head. Corrections are logged by
correction.py, the judge state of each locally judged stop by no-quit.py (learned/states.jsonl); the owner reviews
them with `realstop learn` (scripts/realstop_learn.py), and only a confirmation changes anything. This hook only prunes
learned/states.jsonl: a state older than 30 days is dropped unless a confirmed example refers to it. Fails open.
Python 3.9.
"""
from __future__ import annotations

import json
import os
import time

HOME = os.path.expanduser("~/.claude/realstop")
KEEP_S = 30 * 86400


def rows(path: str) -> list:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def err(e) -> None:
    """A swallowed exception leaves its type in errors.jsonl (never prompt or reply text), so a broken hook shows."""
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(os.path.join(HOME, "errors.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": int(time.time()), "hook": "learn", "where": "main", "type": type(e).__name__}) + "\n")
    except OSError:
        pass


def main() -> None:
    try:
        d = os.path.join(HOME, "learned")
        path = os.path.join(d, "states.jsonl")
        allr = [r for r in rows(path) if isinstance(r, dict)]
        keep = {e.get("state_id") for e in rows(os.path.join(d, "examples.jsonl")) if isinstance(e, dict)}
        kept = [r for r in allr if r.get("id") in keep or time.time() - r.get("at", 0) < KEEP_S]
        if len(kept) != len(allr):
            tmp = "%s.tmp%d" % (path, os.getpid())
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(r) + "\n" for r in kept))
            os.replace(tmp, path)
    except Exception as e:
        err(e)
        return


if __name__ == "__main__":
    main()
