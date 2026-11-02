"""Where things live. Resolved at call time (never at import), so a changed HOME is honored. Python 3.9+, stdlib only."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

OLD_NAME = "resilient"  # the working name of the pre-rename layout
LABEL = "com.realstop.model-server"


def res_root() -> Path:
    """The folder holding hooks/, scripts/, data/, skills/ and CHANGELOG.md: bundled inside an installed wheel
    (realstop/_res), or the clone's top folder when run from source."""
    here = Path(os.path.abspath(__file__)).parent  # not resolve(): a symlinked clone path must stay as the owner knows it
    bundled = here / "_res"
    return bundled if bundled.is_dir() else here.parent


def claude_dir() -> Path:
    return Path.home() / ".claude"


def dest() -> Path:
    return claude_dir() / "realstop"


def old_dest() -> Path:
    return claude_dir() / OLD_NAME


def settings_path() -> Path:
    return claude_dir() / "settings.json"


def skill_dir() -> Path:
    return claude_dir() / "skills" / "realstop"


def old_skill_dir() -> Path:
    return claude_dir() / "skills" / OLD_NAME


def agent_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def _ok39(py: str) -> bool:
    try:
        return subprocess.run([py, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"],
                              capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


def hook_python() -> str:
    """The interpreter the hooks are registered with: the system one (it survives this tool being reinstalled), else
    python3 on PATH, else the one running this command. Always >= 3.9."""
    import shutil
    for cand in ("/usr/bin/python3", shutil.which("python3"), sys.executable):
        if cand and Path(cand).exists() and _ok39(cand):
            return cand
    return sys.executable


def helper(name: str) -> str:
    return str(res_root() / "scripts" / name)


def run_helper(script: str, *args: str, py: Optional[str] = None) -> int:
    """Run one of the bundled scripts/ helpers isolated (-I); its output goes straight through."""
    sys.stdout.flush()
    return subprocess.call([py or sys.executable, "-I", helper(script), *args])
