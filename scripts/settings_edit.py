#!/usr/bin/env python3
"""Safe edits of Claude Code's settings.json for RealStop: register, unregister, check. Python 3.9.

  settings_edit.py check|register|unregister --settings PATH [--py PYTHON --dest DIR]

Guarantees:
  - A settings file that is not valid JSON, or whose "hooks" shape is not what Claude Code writes, is never modified:
    exit 3 with a plain explanation (it is not repaired, replaced or backed over).
  - Edits take an exclusive lock (settings.json.realstop-lock), write a temp file in the same folder, fsync it,
    rename it over the original and fsync the folder. A crash leaves the old file or the new one, never half of one.
  - Only RealStop's own hook entries are added or removed; every other key, hook and group is kept as it was.
  - Idempotent: a run that changes nothing writes nothing and makes no backup. A run that changes the file first
    copies it to settings.json.bak-realstop-<timestamp>.
  - Double registration guard: register adds nothing when a RealStop plugin is enabled (enabledPlugins), and
    removes copies of its own entries before re-adding them once. (Plugin installed later: the hooks themselves
    back off from a second run of the same event, see correction.py claimed() and no-quit.py duplicate().)
Ceiling: other programs writing settings.json do not take this lock, so a write by one of them in the instant
between our read and our rename can be lost. The lock only serializes RealStop's own installers.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sys
import time

EVENTS = (("Stop", "no-quit.py", 20), ("SubagentStop", "no-quit.py", 20),
          ("UserPromptSubmit", "correction.py", 5), ("SessionStart", "learn.py", 10))
# a RealStop hook command: <...>/realstop/<script> (or the legacy .../resilient/<script>) (install.sh) or <plugin root>/.../<script> (plugin-style)
MINE = re.compile(r"(?:[/\\](?:resilient|realstop)[^\s\"']*|CLAUDE_PLUGIN_ROOT[^\s\"']*)[/\\](?:no-quit|correction|learn)\.py(?:\s|$|[\"'])")


class Refuse(Exception):
    pass


def is_mine(h) -> bool:
    return isinstance(h, dict) and bool(MINE.search(str(h.get("command", "")) + " "))


def load(path: str):
    """The parsed settings, or {} when the file does not exist. Raises Refuse on anything unexpected."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise Refuse("cannot read %s (%s)" % (path, e))
    if not raw.strip():
        raise Refuse("%s is empty" % path)
    try:
        s = json.loads(raw)
    except ValueError as e:
        raise Refuse("%s is not valid JSON (%s)" % (path, e))
    if not isinstance(s, dict):
        raise Refuse("%s is not a JSON object" % path)
    hooks = s.get("hooks", {})
    if not isinstance(hooks, dict):
        raise Refuse('"hooks" in %s is not an object' % path)
    for ev, groups in hooks.items():
        if not isinstance(groups, list) or not all(isinstance(g, dict) and isinstance(g.get("hooks", []), list) for g in groups):
            raise Refuse('"hooks.%s" in %s does not have the shape Claude Code writes' % (ev, path))
    return s


def plugin_enabled(s: dict) -> str:
    ep = s.get("enabledPlugins")
    if isinstance(ep, dict):
        for k, v in ep.items():
            if v and re.match(r"(realstop|resilient)\b", str(k).lower()):
                return str(k)
    return ""


def strip_mine(hooks: dict) -> None:
    """Remove RealStop's entries from its four events only; groups that held anything else keep it. An event list
    is dropped only when this removal emptied it."""
    for ev, _, _ in EVENTS:
        if ev not in hooks:
            continue
        groups, removed = [], False
        for g in hooks[ev]:
            kept = [h for h in g.get("hooks", []) if not is_mine(h)]
            removed = removed or len(kept) != len(g.get("hooks", []))
            if kept or "hooks" not in g:
                groups.append(dict(g, hooks=kept) if "hooks" in g else g)
        if groups or not removed:
            hooks[ev] = groups
        else:
            del hooks[ev]


def count_mine(s: dict) -> dict:
    out = {}
    for ev, _, _ in EVENTS:
        out[ev] = sum(1 for g in s.get("hooks", {}).get(ev, []) for h in g.get("hooks", []) if is_mine(h))
    return out


def edited(s: dict, action: str, py: str, dest: str):
    import copy
    n = copy.deepcopy(s)
    hooks = n.setdefault("hooks", {})
    strip_mine(hooks)
    note = ""
    if action == "register":
        plug = plugin_enabled(n)
        if plug:
            note = "plugin %s is enabled and registers the hooks itself, so settings.json carries none of its own (no double registration)" % plug
        else:
            for ev, script, timeout in EVENTS:
                hooks.setdefault(ev, []).append({"hooks": [{"type": "command", "command": "%s -I %s/%s" % (py, dest, script), "timeout": timeout}]})
    if not hooks and "hooks" not in s:
        n.pop("hooks", None)
    elif not hooks:
        n["hooks"] = {}
    return n, note


def action_noop(action: str, n: dict) -> bool:
    return action != "register" or not n.get("hooks")


def write_atomic(path: str, data: str, mode: int) -> None:
    d = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(d, ".settings.json.realstop-tmp-%d" % os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.environ.get("REALSTOP_TEST_CRASH_BEFORE_RENAME"):
            os._exit(9)  # test hook: simulate a kill between the temp write and the rename
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("check", "register", "unregister"))
    ap.add_argument("--settings", required=True)
    ap.add_argument("--py", default="/usr/bin/python3")
    ap.add_argument("--dest", default=os.path.expanduser("~/.claude/realstop"))
    a = ap.parse_args()
    path = a.settings
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".realstop-lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            s = load(path)
        except Refuse as e:
            print("RealStop: %s.\nIt was left exactly as it is: nothing was changed or backed up. Fix the file (or move it "
                  "aside) and run the installer again." % e, file=sys.stderr)
            return 3
        for f in os.listdir(os.path.dirname(os.path.abspath(path))):  # a temp left by a killed run; we hold the lock
            if f.startswith(".settings.json.realstop-tmp-"):
                try:
                    os.unlink(os.path.join(os.path.dirname(os.path.abspath(path)), f))
                except OSError:
                    pass
        if a.action == "check":
            return 0
        n, note = edited(s, a.action, a.py, a.dest)
        if note:
            print("note: " + note)
        if not os.path.exists(path) and action_noop(a.action, n):
            print("ok: no settings.json, nothing to do")
            return 0
        if n == s and os.path.exists(path):
            print("ok: settings.json already up to date (no change, no backup)")
            return 0
        if os.path.exists(path):
            bak = "%s.bak-realstop-%s" % (path, time.strftime("%Y%m%d%H%M%S"))
            i = 0
            while os.path.exists(bak):
                i += 1
                bak = "%s.bak-realstop-%s-%d" % (path, time.strftime("%Y%m%d%H%M%S"), i)
            shutil.copy2(path, bak)
            mode = os.stat(path).st_mode & 0o777
            print("ok: backup %s" % bak)
        else:
            mode = 0o600
        write_atomic(path, json.dumps(n, indent=2, ensure_ascii=False) + "\n", mode)
        print("ok: settings.json %s" % ("updated: RealStop hooks registered" if a.action == "register" and not note else "updated"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
