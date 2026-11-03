"""Install and manage the Codex CLI adapter independently of the Claude install."""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import sqlite3
import tempfile
from pathlib import Path

from . import paths

EVENTS = ("UserPromptSubmit", "PostToolUse", "Stop")


def write(path, data):
    if path.is_symlink():
        raise ValueError("refusing to replace symlink: %s" % path)
    if path.is_file() and path.read_bytes() == data:
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def save(path, obj):
    write(path, (json.dumps(obj, indent=2) + "\n").encode())


def command(args):
    home = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex").resolve()
    dest, hooks = home / "realstop", home / "hooks.json"
    conf = dest / "codex.json"
    if args.cmd != "setup" and not conf.exists() and args.cmd != "uninstall":
        print("Codex adapter is not installed.")
        return 0
    home.mkdir(exist_ok=True, parents=True)
    with (home / ".realstop-install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = json.loads(conf.read_text()) if conf.exists() else {"mode": "shadow", "paused": False}
        if args.cmd == "status":
            print("Codex CLI | rules | %s | %s" % (cfg["mode"], "paused" if cfg["paused"] else "enabled"))
            if (dest / "events.sqlite3").exists():
                with sqlite3.connect(dest / "events.sqlite3") as db:
                    for result, count in db.execute("SELECT result,n FROM totals ORDER BY result"):
                        print("%s: %s" % (result, count))
            print("Check registration and trust with /hooks in Codex.")
            return 0
        if args.cmd in ("pause", "resume"):
            cfg["paused"] = args.cmd == "pause"
            save(conf, cfg)
            return 0
        original = json.loads(hooks.read_text()) if hooks.exists() else {}
        if not isinstance(original, dict) or not isinstance(original.get("hooks", {}), dict):
            raise ValueError("invalid hooks.json")
        groups = original.get("hooks", {})
        script = str(dest / "codex-stop.py")
        runner = shlex.join([paths.hook_python(), "-I", script])
        for event in EVENTS:
            kept = []
            if not isinstance(groups.get(event, []), list):
                raise ValueError("invalid hook group")
            for group in groups.get(event, []):
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    raise ValueError("invalid hook group")
                retained = []
                for handler in group["hooks"]:
                    cmd = handler.get("command") if isinstance(handler, dict) else None
                    try:
                        argv = shlex.split(cmd) if isinstance(cmd, str) else []
                    except ValueError:
                        argv = []
                    if len(argv) != 3 or argv[1:] != ["-I", script]:
                        retained.append(handler)
                if retained or not group["hooks"]:
                    kept.append(dict(group, hooks=retained))
            if args.cmd == "setup":
                kept.append({"hooks": [{"type": "command", "command": runner, "timeout": 5}]})
            if kept:
                groups[event] = kept
            else:
                groups.pop(event, None)
        original["hooks"] = groups
        if args.cmd == "setup":
            dest.mkdir(exist_ok=True, mode=0o700)
            for name in ("codex-stop.py", "no-quit.py"):
                write(dest / name, (paths.res_root() / "hooks" / name).read_bytes())
            cfg["mode"] = args.mode or cfg["mode"]
            save(conf, cfg)
            save(hooks, original)
            print("Installed Codex rules adapter (%s). Open /hooks in Codex and review the three hooks." % cfg["mode"])
        else:
            if hooks.exists():
                save(hooks, original)
            for name in ("codex-stop.py", "no-quit.py", "codex.json", "events.sqlite3", "events.sqlite3-journal"):
                (dest / name).unlink(missing_ok=True)
            if dest.exists() and not any(dest.iterdir()):
                dest.rmdir()
            print("Removed Codex adapter.")
    return 0
