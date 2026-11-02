#!/usr/bin/env python3
"""Print a redacted RealStop summary that is safe to paste into a bug report. Python 3.9.

  python3 scripts/diagnose.py [--home DIR] [--claude-dir DIR] [--last N]     (or: realstop diagnose)

Prints: versions, judge, config KEYS (values only for a short allow-list of non-sensitive settings), hook
registration status, tamper-manifest status, bypass state, and the last N decisions as path, judge and rule labels
only. It never prints reply text, request text, settings.json contents, keys, URLs, session ids or file paths
(your home folder shows as ~). Read the output once before pasting it anywhere public.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time

SAFE_VALUES = ("judge", "strength", "cloud_ok", "shadow", "block_cap", "scope_check")  # enums and numbers, no text
MINE = re.compile(r"(?:[/\\](?:resilient|realstop)[^\s\"']*|CLAUDE_PLUGIN_ROOT[^\s\"']*)[/\\](no-quit|correction|learn)\.py(?:\s|$|[\"'])")
EVENTS = ("Stop", "SubagentStop", "UserPromptSubmit", "SessionStart")
LABEL = re.compile(r"^[a-z][a-z ,'-]{0,39}$")


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def version(here):
    try:
        out = subprocess.run(["git", "-C", here, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return "git " + out.stdout.strip()
    except Exception:
        pass
    try:
        for line in open(os.path.join(here, "CHANGELOG.md"), encoding="utf-8"):
            if line.startswith("## "):
                return line[3:].strip()
    except OSError:
        pass
    return "unknown"


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=os.path.expanduser("~/.claude/realstop"))
    ap.add_argument("--claude-dir", default=os.path.expanduser("~/.claude"))
    ap.add_argument("--last", type=int, default=10)
    a = ap.parse_args()
    home = a.home
    lines = ["RealStop diagnostics (redacted; no reply text, settings contents, keys or paths)", ""]
    lines.append("version: %s" % version(here))
    try:
        from importlib.metadata import version as pkgver
        lines.append("package: realstop %s" % pkgver("realstop"))
    except Exception:
        pass  # run from a clone, not installed as a package
    lines.append("python: %s   os: %s %s" % (platform.python_version(), platform.system(), platform.release()))
    cfg = load(os.path.join(home, "config.json"))
    if cfg is None:
        lines.append("config: missing or unreadable")
        cfg = {}
    else:
        lines.append("config keys: " + ", ".join(sorted(cfg)))
        lines.append("config values: " + ", ".join("%s=%s" % (k, cfg[k]) for k in SAFE_VALUES if k in cfg and isinstance(cfg[k], (bool, int, str)) and len(str(cfg[k])) < 20))
    inst = load(os.path.join(home, "install.json")) or {}
    if inst.get("installed_at"):
        days = (time.time() - float(inst["installed_at"])) / 86400
        gentle = (not inst.get("strength_set")) and days < 7
        lines.append("install age: %.1f days; gentle mode (1 nudge per turn): %s" % (days, "on" if gentle else "off"))
    else:
        lines.append("install age: unknown (older install); gentle mode: off")
    ctl = load(os.path.join(home, "control.json")) or {}
    lines.append("bypass: %s" % ("PAUSED" if ctl.get("paused") is True else "active (not paused)"))
    # hook registration (counts only)
    s = load(os.path.join(a.claude_dir, "settings.json"))
    if s is None:
        lines.append("settings.json: missing, unreadable or not valid JSON")
    else:
        hooks = s.get("hooks", {}) if isinstance(s.get("hooks"), dict) else {}
        counts = {}
        for ev in EVENTS:
            counts[ev] = sum(1 for g in hooks.get(ev, []) if isinstance(g, dict) for h in g.get("hooks", []) if isinstance(h, dict) and MINE.search(str(h.get("command", "")) + " "))
        lines.append("hook entries in settings.json: " + ", ".join("%s=%d" % (e, counts[e]) for e in EVENTS))
        ep = s.get("enabledPlugins") if isinstance(s.get("enabledPlugins"), dict) else {}
        plug = [k.split("@")[0] for k, v in ep.items() if v and re.match(r"(realstop|resilient)\b", k.lower())]
        lines.append("RealStop plugin enabled: %s" % ("yes (" + ", ".join(plug) + ")" if plug else "no"))
        total = sum(counts.values())
        if plug and total:
            lines.append("WARNING: plugin and settings.json entries are both present (the hooks back off from a duplicate run; run realstop uninstall or disable one)")
        elif any(c > 1 for c in counts.values()):
            lines.append("WARNING: an event has more than one RealStop entry; run: realstop setup")
        elif total == 0 and not plug:
            lines.append("WARNING: no RealStop hooks are registered; run: realstop setup")
    # tamper manifest
    man = load(os.path.join(home, "manifest.json"))
    if man is None:
        lines.append("manifest: missing")
    else:
        md = str(cfg.get("model_dir") or "")  # thresholds.json is hashed from the model folder, as the hook does (absent = None)
        bad = [n for n, h in (man.get("files") or {}).items() if h and (sha(os.path.join(md, n)) if n == "thresholds.json" and os.path.isabs(md) else None if n == "thresholds.json" else sha(os.path.join(home, n))) != h and n != "pack.json"]
        lines.append("manifest: %s" % ("matches the installed hooks and config" if not bad else "MISMATCH in " + ", ".join(sorted(bad))))
    lines.append("local model present: %s" % ("yes" if cfg.get("model_dir") and os.path.isfile(os.path.join(str(cfg.get("model_dir")), "pack.json")) else "no"))
    # last decisions: path, judge, block, rule labels, no text
    rows = []
    try:
        with open(os.path.join(home, "stops.jsonl"), encoding="utf-8") as f:
            for line in f.readlines()[-max(a.last, 0):]:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    lines.append("")
    lines.append("last %d decisions (UTC; path, judge, blocked, rule labels):" % len(rows))
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r.get("at", 0)))
        labels = []
        for h in r.get("hits") or []:
            lab = str(h).split(":")[0].strip().lower()
            labels.append(lab if LABEL.match(lab) else "other")
        extra = [k for k in ("bypass", "held", "live_stop", "shadow", "handoff", "tamper") if r.get(k)]
        lines.append("  %s  path=%s judge=%s blocked=%s hits=[%s]%s" % (
            when, r.get("path", "-"), r.get("judge", "-"), "yes" if r.get("block") else "no", "; ".join(labels),
            ("  " + " ".join(k if k != "bypass" else "bypass=" + str(r["bypass"]) for k in extra)) if extra else ""))
    out = "\n".join(lines) + "\n"
    out = out.replace(os.path.expanduser("~"), "~")
    sys.stdout.write(out)


if __name__ == "__main__":
    main()
