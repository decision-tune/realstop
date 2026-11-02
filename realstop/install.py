"""setup / uninstall / pause / keep-server: a port of the old install.sh, same files, settings edits, backups, manifest
and migration. The hooks stay in ~/.claude/realstop. Python 3.9+, stdlib only; no shell."""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from . import paths

STANDING = """# Standing approvals: steps you have already approved for good. One phrase or regex per line (case-insensitive).
# When the agent hands you one of these, or asks consent for it, the Stop hook blocks the stop and tells it you
# pre-approved it: it runs the step itself and hands it over only if its own retry is denied. Examples:
# queue approve
# task \\S+ approve
# merge release PRs
"""

MENU = """
Which judge should RealStop use to decide whether a stop is a quit?
  1) Private and local (recommended if your code should stay on your machine): a small model on your Mac,
     nothing leaves your machine. [default]
  2) Cloud is fine (similar catch rate to the local model in our tests, more wrong nudges, paid per use,
     your stop text goes to its provider): the cloud decision model.
  3) Rules only: no model, nothing to download."""


def say(msg: str = "", err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout, flush=True)


def load_json(p: Path):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_json_atomic(p: Path, data, **kw) -> None:
    tmp = p.with_name(p.name + ".tmp%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, **kw)
    os.replace(tmp, p)


def sha(p: Path) -> Optional[str]:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def settings_edit(action: str, py: str) -> int:
    return paths.run_helper("settings_edit.py", action, "--settings", str(paths.settings_path()),
                            *(["--py", py, "--dest", str(paths.dest())] if action != "check" else []))


# ---- migration from the pre-rename layout ----------------------------------------------------------------------------
def needs_migration() -> bool:
    old, dest = paths.old_dest(), paths.dest()
    return old.is_dir() and not old.is_symlink() and not dest.exists() and not dest.is_symlink()


def _drop_old_skill() -> None:
    f = paths.old_skill_dir() / "SKILL.md"
    try:
        if f.is_file() and "name: resilient" in f.read_text(encoding="utf-8").splitlines():
            f.unlink()
            try:
                paths.old_skill_dir().rmdir()
            except OSError:
                pass
    except OSError:
        pass


def migrate(py: str) -> int:
    """Move an earlier build's ~/.claude/resilient into ~/.claude/realstop in one rename, point config.json's paths at
    it, and rewrite RealStop's own hook entries. Never touches an existing ~/.claude/realstop (so never overwrites
    standing.txt). Call after the settings.json check, so a refusal changes nothing."""
    if not needs_migration():
        return 0
    old, dest = paths.old_dest(), paths.dest()
    try:
        os.rename(old, dest)
    except OSError:
        return 1
    cfg = dest / "config.json"
    c = load_json(cfg)  # no config.json (or not JSON): nothing to rewrite, and never repaired here
    if isinstance(c, dict):
        o, n = str(old), str(dest)
        for k, v in list(c.items()):
            if isinstance(v, str) and (v == o or v.startswith(o + os.sep)):
                c[k] = n + v[len(o):]
        write_json_atomic(cfg, c)
    if settings_edit("register", py) != 0:
        return 1
    _drop_old_skill()
    say("ok: migrated your earlier install: %s moved to %s (config, standing.txt, logs, learned files); hook entries in "
        "settings.json now point there; the old skill folder was replaced" % (old, dest))
    return 0


# ---- the cloud key -------------------------------------------------------------------------------------------------------
def have_key() -> bool:
    """A cloud key is available (env, 0600 file or secrets store); never prints it."""
    if os.environ.get("TYPESAFE_API_KEY"):
        return True
    try:
        if (paths.dest() / "typesafe.key").stat().st_size > 0:
            return True
    except OSError:
        pass
    if shutil.which("secrets"):
        return subprocess.run(["secrets", "get", "TYPESAFE_API_KEY"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    return False


def store_key(key: str) -> None:
    f = paths.dest() / "typesafe.key"
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as h:
        h.write(key + "\n")
    os.chmod(f, 0o600)


# ---- setup --------------------------------------------------------------------------------------------------------------
def ask_judge(strength: str) -> tuple:
    """The first-run menu. Returns (judge, strength). 2 without a key falls back to 1, an unknown answer to 1."""
    judge = "local"
    say(MENU)
    sys.stdout.write("Choose 1, 2 or 3 [1]: ")
    sys.stdout.flush()
    ans = sys.stdin.readline().rstrip("\n")
    if not sys.stdin.isatty():
        say()
    if ans == "3":
        return "phrases", ""
    if ans == "2":
        have = have_key()
        if not have:
            prompt = "Paste your cloud decision model key (hidden, stored 0600 on this machine; just Enter to skip): "
            if sys.stdin.isatty():
                ukey = getpass.getpass(prompt)
            else:
                sys.stdout.write(prompt)
                sys.stdout.flush()
                ukey = sys.stdin.readline().rstrip("\n")
                say()
            if ukey:
                store_key(ukey)
                say("ok: cloud key stored (0600)")
                have = True
        if have:
            return "jev", ""
        say("install: no cloud key given, so using choice 1 (private and local)")
    elif ans not in ("", "1"):
        say("install: '%s' is not 1, 2 or 3, so using choice 1 (private and local)" % ans)
    return judge, strength


def fetch_model(default_model: Path) -> None:
    rc = paths.run_helper("fetch_model.py", "--dest", str(default_model))
    if rc != 0:
        if (default_model / "pack.json").is_file():
            say("install: note: the local model at %s could not be verified against the pinned file list "
                "(see MODEL.md); it is left as it is" % default_model, err=True)
        else:
            say("install: note: the model download did not complete; the phrase rules are active (see MODEL.md)", err=True)


def write_manifest(dest: Path) -> None:
    """Tamper manifest (last, after every write to the hooks and config.json); config.json absent = null."""
    man = dest / "manifest.json"
    try:
        man.unlink()
    except FileNotFoundError:
        pass
    files = {n: sha(dest / n) for n in ("no-quit.py", "correction.py", "learn.py", "realstop_learn.py", "config.json")}
    c = load_json(dest / "config.json")
    md = str((c or {}).get("model_dir") or "") if isinstance(c, dict) else ""
    files["pack.json"] = sha(Path(md) / "pack.json") if os.path.isabs(md) else None  # the local judge's question and class mixes
    files["thresholds.json"] = sha(Path(md) / "thresholds.json") if os.path.isabs(md) else None  # the model's own strength dials, if any
    with open(man, "w", encoding="utf-8") as f:
        json.dump({"files": files}, f, indent=1)
    os.chmod(man, 0o444)


def local_up(url: str) -> bool:
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=1)
        return True
    except urllib.error.HTTPError:
        return True  # something answered
    except Exception:
        return False


def judge_note(cfg: dict) -> str:
    j, md = cfg.get("judge", "auto"), str(cfg.get("model_dir") or "")
    url = cfg.get("local_url") or "http://127.0.0.1:8765"
    if j in ("local", "auto") and md:
        if not os.path.isfile(os.path.join(md, "pack.json")):
            return "note: judge %s, but there is no local model at %s: the phrase rules are active until it is there and its server runs" % (j, md)
        if local_up(url):
            return "note: the local model is answering at " + url
        return "note: the local model is not running: the phrase rules are active until you start it (realstop serve, or realstop serve --keep)"
    if j == "phrases":
        return "note: the phrase rules are the judge"
    if j == "jev":
        return "note: the cloud decision model (opt-in) is the judge; the phrase rules back it up"
    return "note: no local model configured: the phrase rules are active"


def setup(judge: str = "", strength: str = "", port: str = "", model: str = "", key_stdin: bool = False,
          keep_server: bool = False, ask: Optional[bool] = None, verify_model: bool = True) -> int:
    """judge is the stored name (local, jev, phrases, auto) or "". ask: None = ask only on a terminal (or REALSTOP_ASK)."""
    py = paths.hook_python()
    dest, res, settings = paths.dest(), paths.res_root(), paths.settings_path()
    default_model = dest / "model" / "realstop-quit"
    strength_arg = strength
    rc = settings_edit("check", py)  # an invalid settings.json stops the install before anything is copied or written
    if rc != 0:
        return rc
    if migrate(py) != 0:
        say("install: migrating %s to %s failed; nothing else was changed" % (paths.old_dest(), dest), err=True)
        return 1
    cfgp = dest / "config.json"
    fresh = not cfgp.exists() and not (dest / "install.json").exists()  # a first install: gentle for a week
    dest.mkdir(parents=True, exist_ok=True)
    paths.skill_dir().mkdir(parents=True, exist_ok=True)
    for n in ("no-quit.py", "correction.py", "learn.py"):
        shutil.copy(res / "hooks" / n, dest / n)
    # `realstop learn`: the launcher, its code and the fixed regression pairs its gate uses
    shutil.copy(res / "scripts" / "realstop", dest / "realstop")
    shutil.copy(res / "scripts" / "realstop_learn.py", dest / "realstop_learn.py")
    shutil.copy(res / "data" / "regression_pairs.jsonl", dest / "regression_pairs.jsonl")
    os.chmod(dest / "realstop", 0o755)
    shutil.copy(res / "skills" / "realstop" / "SKILL.md", paths.skill_dir() / "SKILL.md")
    if not (dest / "standing.txt").exists():  # the owner's standing approvals: created once, never overwritten
        (dest / "standing.txt").write_text(STANDING, encoding="utf-8")
    # default judge: only when nothing is stored yet (a re-run keeps the owner's choice and never asks again)
    stored = load_json(cfgp)
    if not judge and not (isinstance(stored, dict) and stored.get("judge")):
        judge = "local"
        if not strength:
            strength = "moderate"
        interactive = ask if ask is not None else (sys.stdin.isatty() or bool(os.environ.get("REALSTOP_ASK") or os.environ.get("RESILIENT_ASK")))
        if not key_stdin and interactive:
            judge, strength = ask_judge(strength)
        else:
            say("install: no judge chosen: local model with the phrase rules as backup. To change it: realstop judge local|jev|rules")
    if judge == "local":
        port = port or "8765"
        model = model or str(default_model)
    if model:
        model = os.path.abspath(model) if os.path.isdir(model) else model
    if judge or strength or port or model:
        c = stored if isinstance(stored, dict) else {}
        if judge:
            c["judge"] = judge
        if judge == "jev":
            c["cloud_ok"] = True  # the explicit cloud opt-in; never inferred from a key being present
        elif judge in ("phrases", "local"):
            c["cloud_ok"] = False
        if strength:
            c["strength"] = strength  # the thresholds live in no-quit.py (DT_THRESHOLDS), covered by the manifest
        if port:
            c["local_url"] = "http://127.0.0.1:%d" % int(port)  # loopback only; the hook refuses anything else
        if model:
            c["model_dir"] = model
        with open(cfgp, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=1)
        say("ok: config =%s%s%s%s" % (" judge " + judge if judge else "", " strength " + strength if strength else "",
                                      " port " + port if port else "", " model " + model if model else ""))
    # fetch the model into the default folder (a custom --model folder is the owner's own and is never written to). A
    # model already there is verified against the pinned hashes and not downloaded again when they match. Any failure
    # is non-fatal: the install goes on and says the phrase rules are active.
    cfg = load_json(cfgp)
    cfg = cfg if isinstance(cfg, dict) else {}
    if cfg.get("judge") in ("local", "auto") and str(cfg.get("model_dir") or "") == str(default_model) \
            and (verify_model or not (default_model / "pack.json").is_file()):
        fetch_model(default_model)
    if model and not (Path(model) / "pack.json").is_file():
        say("install: note: no local model at %s (no pack.json); the phrase rules are active until the model is there and its server runs" % model, err=True)
    if key_stdin:
        key = sys.stdin.read().rstrip("\n")
        if not key:
            say("install: empty key on stdin", err=True)
            return 1
        store_key(key)
        say("ok: cloud key stored (0600)")
    if judge == "jev" and not have_key():
        say("note: the cloud decision model needs a key: realstop setup --judge jev --key-stdin (or set TYPESAFE_API_KEY)")
    ij = dest / "install.json"
    d = load_json(ij)
    d = d if isinstance(d, dict) else {}
    if fresh and "installed_at" not in d:
        d["installed_at"] = int(time.time())  # gentle mode: one nudge per user turn for 7 days (hooks/no-quit.py)
    if strength_arg:
        d["strength_set"] = True  # the owner chose a strength: the configured cap applies from now on
    if d:
        write_json_atomic(ij, d)
    rc = settings_edit("register", py)
    if rc != 0:
        return rc
    sys.stdout.flush()
    rc = subprocess.call([sys.executable, "-I", str(dest / "learn.py")])
    if rc != 0:
        return rc
    write_manifest(dest)
    if keep_server:
        rc = install_agent()
        if rc != 0:
            return rc
    key = "no"
    if (dest / "typesafe.key").is_file() and (dest / "typesafe.key").stat().st_size > 0:
        key = "file"
    if key == "no" and shutil.which("secrets"):
        key = "secrets store"
    say("ok: RealStop installed in %s (python %s, cloud key: %s)" % (dest, platform.python_version(), key))
    c = load_json(cfgp)
    say(judge_note(c if isinstance(c, dict) else {}))
    link_command(dest)
    # corrections waiting for review reach you here (and in `realstop status`), never through the agent's context
    subprocess.call([sys.executable, "-I", str(dest / "realstop_learn.py"), "--installer-note"], stderr=subprocess.DEVNULL)
    return 0


def link_command(dest: Path) -> None:
    """The `realstop` command itself is on PATH when the package was installed with uv or pipx. Without it (a source run),
    link the installed launcher onto PATH when ~/.local/bin exists and the name is free or already ours."""
    if shutil.which("realstop"):
        say("ok: `realstop learn` reviews your corrections (review-gated learning); `realstop status` shows where things stand")
        return
    rl = Path.home() / ".local" / "bin" / "realstop"
    ours = rl.is_symlink() and os.readlink(rl) == str(dest / "realstop")
    if rl.parent.is_dir() and (ours or (not rl.exists() and not rl.is_symlink())):
        if ours:
            rl.unlink()
        os.symlink(dest / "realstop", rl)
        say("ok: realstop learn is available as: realstop learn  (review-gated learning from your corrections)")
    else:
        say("note: review-gated learning: run %s learn  (or link it onto your PATH)" % (dest / "realstop"))


# ---- pause / resume -------------------------------------------------------------------------------------------------------
def pause(paused: bool) -> int:
    if needs_migration():  # an earlier-build install: migrate first (a plain setup does it, with its hooks and manifest)
        rc = setup(ask=False)
        if rc != 0:
            return rc
    dest = paths.dest()
    dest.mkdir(parents=True, exist_ok=True)
    p = dest / "control.json"
    c = load_json(p)
    c = c if isinstance(c, dict) else {}
    c["paused"] = paused
    write_json_atomic(p, c)
    if paused:
        say("ok: RealStop is paused: every stop goes through. Turn it back on with: realstop resume (or type: realstop on)")
    else:
        say("ok: RealStop is on again")
    return 0


# ---- the model server LaunchAgent -----------------------------------------------------------------------------------------
def _launchctl_remove() -> None:
    agent = paths.agent_plist()
    uid = str(os.getuid())
    for cmd in (["launchctl", "bootout", "gui/%s/%s" % (uid, paths.LABEL)], ["launchctl", "unload", str(agent)]):
        try:
            if subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                break
        except OSError:
            break


def remove_agent() -> int:
    _launchctl_remove()
    try:
        paths.agent_plist().unlink()
    except FileNotFoundError:
        pass
    say("ok: model server LaunchAgent removed (if it was installed)")
    return 0


def install_agent() -> int:
    """Opt-in: a user LaunchAgent keeps the model server running on 127.0.0.1 only (macOS). It takes the GPU lease per
    request only (scripts/realstop-serve.sh), never for its whole life."""
    if sys.platform != "darwin":
        say("realstop: --keep installs a macOS LaunchAgent; on this system run `realstop serve` yourself (a systemd/Windows service is not built yet)", err=True)
        return 1
    import plistlib
    c = load_json(paths.dest() / "config.json")
    md = str((c or {}).get("model_dir") or "/nonexistent") if isinstance(c, dict) else "/nonexistent"
    if not os.path.isfile(os.path.join(md, "pack.json")):
        say("install: --keep needs a local model (run: realstop judge local, with a model that has pack.json)", err=True)
        return 1
    agent = paths.agent_plist()
    agent.parent.mkdir(parents=True, exist_ok=True)
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"}
    if os.environ.get("DT_PY"):
        env["DT_PY"] = os.environ["DT_PY"]
    log = str(paths.dest() / "server.log")
    with open(agent, "wb") as f:
        plistlib.dump({"Label": paths.LABEL, "ProgramArguments": ["/bin/bash", paths.helper("realstop-serve.sh")],
                       "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30, "StandardOutPath": log,
                       "StandardErrorPath": log, "EnvironmentVariables": env}, f)
    uid = str(os.getuid())
    subprocess.run(["launchctl", "bootout", "gui/%s/%s" % (uid, paths.LABEL)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if subprocess.call(["launchctl", "bootstrap", "gui/" + uid, str(agent)]) != 0:
        if subprocess.call(["launchctl", "load", str(agent)]) != 0:
            return 1
    say("ok: model server LaunchAgent installed (%s, 127.0.0.1 only; remove with: realstop serve --unkeep)" % paths.LABEL)
    return 0


# ---- uninstall ------------------------------------------------------------------------------------------------------------
def uninstall() -> int:
    py = paths.hook_python()
    dest, old = paths.dest(), paths.old_dest()
    rc = settings_edit("unregister", py)  # first: a refusal changes nothing
    if rc != 0:
        return rc
    _launchctl_remove()
    try:
        paths.agent_plist().unlink()
    except FileNotFoundError:
        pass
    rl = Path.home() / ".local" / "bin" / "realstop"
    if rl.is_symlink() and os.readlink(rl) == str(dest / "realstop"):
        rl.unlink()
    try:
        Path(str(paths.settings_path()) + ".realstop-lock").unlink()
    except FileNotFoundError:
        pass
    for d in (dest, old):  # the current folder and the pre-rename one
        if d.is_symlink():
            say("install: %s is a symlink, so its contents were left alone" % d, err=True)
        elif d.is_dir():  # only RealStop's own folder; the user's standing.txt is kept
            for f in d.iterdir():
                if f.name == "standing.txt":
                    continue
                if f.is_dir() and not f.is_symlink():
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    f.unlink()
            try:
                d.rmdir()
            except OSError:
                pass
    try:
        (paths.skill_dir() / "SKILL.md").unlink()
    except FileNotFoundError:
        pass
    try:
        paths.skill_dir().rmdir()
    except OSError:
        pass
    _drop_old_skill()
    say("ok: RealStop uninstalled: hook entries, files in %s, the skill file and the LaunchAgent are gone." % dest)
    for d in (dest, old):
        if (d / "standing.txt").exists():
            say("kept: %s/standing.txt (your own list; delete it by hand if you want)" % d)
    say("kept: settings.json backups (settings.json.bak-realstop-* and bak-resilient-*) and anything outside those folders, such as a custom --model folder")
    say("The realstop command itself is still installed: remove it with `uv tool uninstall realstop` or `pipx uninstall realstop`.")
    return 0
