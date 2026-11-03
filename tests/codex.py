"""Exercise installed Codex commands through their public JSON and CLI interfaces."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CodexTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="codex space ")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.env = dict(os.environ, CODEX_HOME=str(self.home))
        self.hooks = self.home / "hooks.json"
        self.original = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo example"}]}]}}
        self.hooks.write_text(json.dumps(self.original))
        self.cli("setup")

    def cli(self, cmd, *args, check=True):
        return subprocess.run([sys.executable, "-m", "realstop", cmd, "--host", "codex", *args],
                              cwd=ROOT, env=self.env, text=True, capture_output=True, check=check)

    def event(self, name, **fields):
        cmd = json.loads(self.hooks.read_text())["hooks"][name][-1]["hooks"][0]["command"]
        data = dict(session_id="s", turn_id="t", hook_event_name=name, stop_hook_active=False)
        result = subprocess.run(shlex.split(cmd), input=json.dumps(dict(data, **fields)),
                                capture_output=True, text=True, check=True, env=self.env)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout) if result.stdout else {}

    def prompt(self, text, **kw):
        self.event("UserPromptSubmit", prompt=text, **kw)

    def stop(self, text="Docs written. Should I add a changelog entry?", **kw):
        return self.event("Stop", last_assistant_message=text, **kw)

    def test_shadow_and_lifecycle(self):
        self.prompt("Write docs.md and add a changelog entry.")
        self.assertFalse(self.stop())
        self.assertIn("would_block: 1", self.cli("status").stdout)
        self.cli("setup", "--mode", "block")
        self.cli("setup")
        self.cli("pause")
        self.prompt("Add a changelog entry.", turn_id="paused")
        self.assertFalse(self.stop(turn_id="paused"))
        self.cli("resume")
        self.prompt("Add a changelog entry.", turn_id="new")
        self.assertEqual(self.stop(turn_id="new")["decision"], "block")
        self.assertFalse(self.stop(turn_id="new", stop_hook_active=True))
        self.assertFalse(self.stop(turn_id="new"))
        self.cli("uninstall")
        self.assertEqual(json.loads(self.hooks.read_text()), self.original)

    def test_protected_stops(self):
        self.cli("setup", "--mode", "block")
        self.assertFalse(self.stop())
        cases = [
            ("Write docs.md.", "Done. Should I add a changelog entry?", {}),
            ("Add a changelog entry.", "NEEDS-YOU: approval. Should I add a changelog entry?", {}),
            ("Add a changelog entry.", "The docs say “Should I add a changelog entry?”", {}),
            ("Do not add a changelog entry.", "Should I add a changelog entry?", {}),
            ("Add a changelog entry.", "Should I add a changelog entry?", {"permission_mode": "plan"}),
            ("Add a changelog entry.", "Should I add a changelog entry?", {"agent_id": "child"}),
        ]
        for i, (prompt, reply, flags) in enumerate(cases):
            with self.subTest(i=i):
                self.prompt(prompt, turn_id=str(i))
                self.assertFalse(self.stop(reply, turn_id=str(i), **flags))
        self.prompt("Add a changelog entry.")
        self.event("PostToolUse", tool_name="Bash", tool_response={"is_error": True})
        self.assertFalse(self.stop())

    def test_steering_and_invalid_settings(self):
        self.cli("setup", "--mode", "block")
        self.prompt("Add a changelog entry.")
        self.prompt("Stop now.")
        self.assertFalse(self.stop())
        self.hooks.write_text("{bad json")
        self.assertNotEqual(self.cli("setup", check=False).returncode, 0)
        self.assertEqual(self.hooks.read_text(), "{bad json")


if __name__ == "__main__":
    unittest.main()
