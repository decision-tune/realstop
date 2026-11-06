from dataclasses import asdict
from pathlib import Path
import json
import subprocess
import sys
import unittest

from realstop.supervisor.optional_verdict import observe_tool, MAX_COMMAND_CHARS, MAX_STREAM_CHARS


class RecordedPredictor:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"label":"success", "confidence":0.7,
            "probabilities":{"success":0.7,"failure":0.1,"blocked":0.1,"unknown":0.1}}

    def predict(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class OptionalVerdictTests(unittest.TestCase):
    def test_default_does_not_import_optional_package_or_contact_network(self):
        module_dir = str(Path(__file__).resolve().parents[3])
        code = """
import builtins, socket, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith('verdict'):
        raise AssertionError('optional dependency imported')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
def forbidden(*args, **kwargs):
    raise AssertionError('network used')
socket.socket.connect = forbidden
from realstop.supervisor.optional_verdict import observe_tool
result = observe_tool(command='test', exit_code=0)
assert result.status == 'disabled' and result.label == 'unknown'
"""
        subprocess.run([sys.executable, "-c", code, module_dir], check=True,
                       capture_output=True, text=True)
        backend = RecordedPredictor()
        self.assertEqual(observe_tool(command="test", exit_code=0, predictor=backend).status, "disabled")
        self.assertEqual(backend.calls, [])

    def test_explicit_backend_preserves_outcome_without_completion_authority(self):
        backend = RecordedPredictor()
        result = observe_tool(command="test", exit_code=0, stdout="4 passed", stderr="",
                              enabled=True, predictor=backend)
        self.assertEqual(backend.calls, [dict(command="test", exit_code=0, stdout="4 passed", stderr="")])
        self.assertEqual((result.label, result.status, result.confidence_validated),
                         ("success", "ok", False))
        fields = asdict(result)
        self.assertTrue(fields["advisory"])
        self.assertNotIn("task_complete", fields)
        self.assertNotIn("continuation", fields)
        self.assertNotIn("authority", fields)
        self.assertEqual(observe_tool(command="test", exit_code=0, enabled=True).status, "unavailable")

    def test_input_boundaries_and_known_clipping_never_call_backend(self):
        backend = RecordedPredictor()
        exact = dict(command="x" * MAX_COMMAND_CHARS, exit_code=0,
                     stdout="x" * MAX_STREAM_CHARS, stderr="x" * MAX_STREAM_CHARS,
                     enabled=True, predictor=backend)
        self.assertEqual(observe_tool(**exact).status, "ok")
        self.assertEqual(len(backend.calls), 1)
        for field in ("command", "stdout", "stderr"):
            oversized = dict(exact, **{field:exact[field]+"x"})
            result = observe_tool(**oversized)
            self.assertEqual((result.label, result.truncated, result.status),
                             ("unknown", True, "truncated_input"))
        result = observe_tool(**exact, already_truncated=True)
        self.assertTrue(result.truncated)
        self.assertEqual(len(backend.calls), 1)

    def test_malformed_or_nonfinite_probabilities_are_not_advice(self):
        valid = RecordedPredictor().result
        malformed = [None, {"label":"continue"},
                     dict(valid, confidence=float("nan")), dict(valid, confidence=True),
                     dict(valid, confidence=0.9),
                     dict(valid, probabilities={"success":1.0}),
                     dict(valid, probabilities=dict(valid["probabilities"], success=float("inf"))),
                     dict(valid, probabilities=dict(valid["probabilities"], failure=0.8))]
        for response in malformed:
            with self.subTest(response=response):
                backend = RecordedPredictor()
                backend.result = response
                result = observe_tool(command="test", exit_code=0, enabled=True, predictor=backend)
                self.assertEqual((result.label, result.status), ("unknown", "invalid_response"))

    def test_backend_exception_and_invalid_input_do_not_leak_output(self):
        class Broken:
            def predict(self, **kwargs):
                raise RuntimeError("PRIVATE_TOOL_BODY")
        result = observe_tool(command="test", exit_code=0, enabled=True, predictor=Broken())
        self.assertEqual(result.status, "backend_error")
        self.assertNotIn("PRIVATE_TOOL_BODY", json.dumps(asdict(result)))
        for changes in ({"exit_code":True}, {"stdout":None}, {"enabled":"yes"},
                        {"already_truncated":"false"}):
            args = dict(command="test", exit_code=0, enabled=True, predictor=Broken())
            args.update(changes)
            self.assertEqual(observe_tool(**args).status, "invalid_input")


if __name__ == "__main__":
    unittest.main()
