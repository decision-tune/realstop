"""The `realstop` command. Python 3.9+, stdlib only.

  realstop setup | judge | strength | learn | pause | resume | status | diagnose | serve | uninstall
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from . import __version__, install, paths

JUDGES = ("local", "jev", "rules")
STRENGTHS = ("light", "moderate", "hard")


def stored_judge(name) -> str:
    name = name or ""
    return "phrases" if name == "rules" else name  # config.json (and the hooks) say "phrases" for the rules-only judge


def port_arg(v: str) -> str:
    if not v.isdigit() or not 1 <= int(v) <= 65535:
        raise argparse.ArgumentTypeError("must be a number from 1 to 65535")
    return v


def judge_arg(v: str) -> str:
    if v not in JUDGES + ("phrases", "auto"):  # phrases (the stored name) and auto are accepted for old scripts
        raise argparse.ArgumentTypeError("choose local, jev or rules")
    return v


def add_judge_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model", metavar="DIR", default="", help="a local model folder (default: ~/.claude/realstop/model/realstop-quit)")
    p.add_argument("--port", type=port_arg, default=None, help="the local model server port (default 8765)")
    p.add_argument("--key-stdin", action="store_true", help="store the cloud decision model key read from stdin, 0600")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="realstop", description="RealStop: catches coding agents handing back work.")
    ap.add_argument("--version", action="version", version="realstop " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    p = sub.add_parser("setup", help="first-time install (asks which judge); safe to re-run")
    p.add_argument("--judge", type=judge_arg, metavar="local|jev|rules", default=None)
    p.add_argument("--strength", choices=STRENGTHS, default="")
    add_judge_opts(p)
    p.add_argument("--keep-server", action="store_true", help="also install the model server LaunchAgent (macOS)")
    p.add_argument("-y", "--yes", action="store_true", help="never ask; take the default (local, moderate) unless --judge is given")
    p = sub.add_parser("judge", help="choose the judge: local | jev | rules")
    p.add_argument("judge", type=judge_arg, metavar="local|jev|rules")
    add_judge_opts(p)
    p = sub.add_parser("strength", help="set the local judge's dial: light | moderate | hard")
    p.add_argument("strength", choices=STRENGTHS)
    sub.add_parser("learn", help="review-gated learning from your corrections (--list, --undo ID, --reset, --status)", add_help=False)
    sub.add_parser("pause", help="pause RealStop: every stop goes through")
    sub.add_parser("resume", help="turn RealStop back on")
    sub.add_parser("status", help="judge, strength, paused?, server up?, pending corrections, versions")
    p = sub.add_parser("diagnose", help="redacted summary for bug reports; changes nothing")
    p.add_argument("--last", type=int, default=10, help="how many recent decisions to list")
    p = sub.add_parser("serve", help="start the local model server (foreground); --keep installs the LaunchAgent")
    p.add_argument("--keep", action="store_true", help="install a LaunchAgent that keeps the server running (macOS), then exit")
    p.add_argument("--unkeep", action="store_true", help="remove that LaunchAgent and exit")
    p.add_argument("--model", metavar="DIR", default="")
    p.add_argument("--port", type=port_arg, default=None)
    sub.add_parser("uninstall", help="remove RealStop's hook entries and files (your standing.txt is kept)")
    for name in ("setup", "pause", "resume", "status", "uninstall"):
        sub.choices[name].add_argument("--host", choices=("claude", "codex"), default="claude")
    sub.choices["setup"].add_argument("--mode", choices=("shadow", "block"), help="Codex only; default shadow")
    return ap


def cmd_status() -> int:
    dest = paths.dest()
    cfg = install.load_json(dest / "config.json")
    if not (dest / "no-quit.py").is_file():
        print("RealStop is not installed. Run: realstop setup")
        print("realstop %s, python %s" % (__version__, sys.version.split()[0]))
        return 0
    cfg = cfg if isinstance(cfg, dict) else {}
    judge = cfg.get("judge") or "auto"
    ctl = install.load_json(dest / "control.json")
    paused = isinstance(ctl, dict) and ctl.get("paused") is True
    md = str(cfg.get("model_dir") or "")
    url = cfg.get("local_url") or "http://127.0.0.1:8765"
    have_model = bool(md) and (Path(md) / "pack.json").is_file()
    if judge in ("local", "auto") and md:
        server = ("answering at " + url) if have_model and install.local_up(url) else \
                 ("not running (the rules judge until you start it: realstop serve)" if have_model else "no local model at " + md)
    else:
        server = "not needed"
    pending = "unknown"
    learn = dest / "realstop_learn.py"
    if learn.is_file():
        out = subprocess.run([sys.executable, "-I", str(learn), "--status"], capture_output=True, text=True)
        m = re.search(r"pending corrections:\s+(\d+)", out.stdout)
        if m:
            pending = m.group(1)
    here, there = install.sha(paths.res_root() / "hooks" / "no-quit.py"), install.sha(dest / "no-quit.py")
    hooks = "up to date" if here and here == there else "differs from this package (realstop setup updates them)"
    man = install.load_json(dest / "manifest.json")
    mf = (man or {}).get("files", {}) if isinstance(man, dict) else {}
    print("judge:        %s%s" % ("rules" if judge == "phrases" else judge, " (rules back it up)" if judge in ("local", "jev", "auto") else ""))
    print("strength:     %s" % (cfg.get("strength") or "moderate (default)"))
    print("paused:       %s" % ("YES (every stop goes through; realstop resume)" if paused else "no"))
    print("server:       %s" % server)
    print("corrections:  %s waiting for review (realstop learn)" % pending)
    print("versions:     realstop %s, hooks %s (sha %s), python %s" % (__version__, hooks, (mf.get("no-quit.py") or "?")[:8], sys.version.split()[0]))
    return 0


def cmd_serve(a) -> int:
    if a.unkeep:
        return install.remove_agent()
    if a.keep:
        if a.model or a.port:
            print("realstop: with --keep, set --model/--port first with: realstop judge local --model DIR --port N", file=sys.stderr)
            return 2
        return install.install_agent()
    script = paths.helper("realstop-serve.sh")
    # Ceiling: the server launcher is still bash (macOS and Linux); a Python port is the Windows step.
    sys.stdout.flush()
    return subprocess.call(["bash", script] + (["--model", a.model] if a.model else []) + (["--port", a.port] if a.port else []))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["learn"]:  # the learn script owns its own flags and usage
        sys.stdout.flush()
        return subprocess.call([sys.executable, "-I", paths.helper("realstop_learn.py")] + argv[1:])
    a = build_parser().parse_args(argv)  # an unknown command or flag exits 2
    try:
        if getattr(a, "host", "") == "codex":
            from . import codex
            if a.cmd == "setup" and (a.judge not in (None, "rules", "phrases") or a.strength or a.model or a.port or a.key_stdin or a.keep_server):
                print("Codex supports the rules judge only.", file=sys.stderr)
                return 2
            try:
                return codex.command(a)
            except (ValueError, OSError) as exc:
                print("RealStop Codex: %s" % exc, file=sys.stderr)
                return 2
        if a.cmd == "setup" and a.mode:
            build_parser().error("--mode requires --host codex")
        if a.cmd == "setup":
            return install.setup(stored_judge(a.judge), a.strength, a.port or "", a.model, a.key_stdin, a.keep_server,
                                 ask=False if a.yes else None)
        if a.cmd == "judge":
            return install.setup(stored_judge(a.judge), "", a.port or "", a.model, a.key_stdin, ask=False, verify_model=False)
        if a.cmd == "strength":
            return install.setup("", a.strength, ask=False, verify_model=False)
        if a.cmd == "pause":
            return install.pause(True)
        if a.cmd == "resume":
            return install.pause(False)
        if a.cmd == "status":
            return cmd_status()
        if a.cmd == "diagnose":
            return paths.run_helper("diagnose.py", "--last", str(a.last))
        if a.cmd == "serve":
            return cmd_serve(a)
        if a.cmd == "uninstall":
            return install.uninstall()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())
