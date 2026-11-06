"""Artifact mutation calibration; no agent/controller performance comparison."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runner import create_run, evaluate_run


class WorkUnitCalibration(unittest.TestCase):
    def setUp(self):
        self.root = create_run()

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, task, name, content):
        (self.root/task/name).write_text(content)

    def receipt(self, task, target, check):
        digest = hashlib.sha256((self.root/task/target).read_bytes()).hexdigest()
        self.write(task, "receipt.json", json.dumps({"artifact_sha256":digest,
                   "check":check, "environment":"fixture-local", "result":"pass"}))

    def status(self, task):
        return next(o for o in evaluate_run(self.root)["outcomes"] if o["task_id"] == task)["status"]

    def complete_original_artifacts(self):
        # Literal calibration answers, inspected independently of runner validators.
        # No task-manifest expected value is loaded/copied to create these answers.
        self.write("wu-text-normalize", "normalized.txt", "ALPHA\nBETA\n")
        self.write("wu-json-enable", "settings.json", '{"enabled":true,"retries":3,"tenant":"amber"}')
        self.write("wu-csv-total", "total.txt", "total_cents=310\n")  # 3*20 + 2*125.
        self.write("wu-copy-receipt", "copied.txt", "receipt payload v1\n")
        self.receipt("wu-copy-receipt", "copied.txt", "copy-integrity")
        self.write("wu-two-artifacts", "summary.txt", "amber=2\n")
        self.write("wu-two-artifacts", "count.txt", "2\n")
        self.write("wu-unique-claims", "claims.jsonl",
            '{"obligation_id":"invoice-publish","evidence_digest":"fixed-evidence-B","action_id":"write-publication-report"}\n'
            '{"obligation_id":"invoice-total","evidence_digest":"fixed-evidence-A","action_id":"calculate-total"}\n')
        self.write("wu-revision-receipt", "report.json", '{"version":2,"passed":true}\n')
        self.receipt("wu-revision-receipt", "report.json", "report-v2")
        self.write("wu-wait-callback", "waiting.json", '{"callback_id":"cb-17","status":"waiting"}')

    def test_known_artifacts_pass_and_wait_is_not_counted_as_completion(self):
        self.assertEqual(evaluate_run(self.root)["counts"], {"met":0,"unmet":8,"waiting":0,"invalid":0})
        self.complete_original_artifacts()
        result = evaluate_run(self.root)
        self.assertEqual(result["counts"], {"met":7,"unmet":0,"waiting":1,"invalid":0})
        self.assertFalse(result["performance_claim"])
        self.assertFalse(result["cost_caps_enforced_by_runner"])

    def test_partial_bundle_and_wrong_calculation_are_unmet(self):
        self.write("wu-two-artifacts", "summary.txt", "amber=2\n")
        self.assertEqual(self.status("wu-two-artifacts"), "unmet")
        self.write("wu-csv-total", "total.txt", "total_cents=185\n")
        self.assertEqual(self.status("wu-csv-total"), "unmet")
        self.write("wu-csv-total", "total.txt", "total_cents=310\n")
        self.assertEqual(self.status("wu-csv-total"), "met")
        self.write("wu-csv-total", "items.csv", "item,units,cents\nclip,1,20\n")
        self.assertEqual(self.status("wu-csv-total"), "unmet")

    def test_actual_copy_mutation_invalidates_receipt_even_with_claimed_pass(self):
        self.complete_original_artifacts()
        self.write("wu-copy-receipt", "copied.txt", "tampered content\n")
        self.assertEqual(self.status("wu-copy-receipt"), "unmet")
        self.receipt("wu-copy-receipt", "copied.txt", "copy-integrity")
        self.assertEqual(self.status("wu-copy-receipt"), "unmet")

    def test_stale_revision_and_wrong_check_environment_fail(self):
        self.complete_original_artifacts()
        self.write("wu-revision-receipt", "report.json", '{"version":3,"passed":true}\n')
        self.assertEqual(self.status("wu-revision-receipt"), "unmet")
        self.receipt("wu-revision-receipt", "report.json", "report-v2")
        self.assertEqual(self.status("wu-revision-receipt"), "unmet")
        self.write("wu-revision-receipt", "report.json", '{"version":2,"passed":true}\n')
        self.receipt("wu-revision-receipt", "report.json", "tests")
        self.assertEqual(self.status("wu-revision-receipt"), "unmet")
        self.receipt("wu-revision-receipt", "report.json", "report-v2")
        receipt_path = self.root/"wu-revision-receipt/receipt.json"
        receipt = json.loads(receipt_path.read_text()); receipt["environment"] = "other"
        receipt_path.write_text(json.dumps(receipt))
        self.assertEqual(self.status("wu-revision-receipt"), "unmet")

    def test_duplicate_claim_delivery_is_detected_from_actual_jsonl(self):
        self.complete_original_artifacts()
        path = self.root/"wu-unique-claims/claims.jsonl"
        self.assertEqual(self.status("wu-unique-claims"), "met")
        path.write_text(path.read_text()+path.read_text().splitlines()[0]+"\n")
        self.assertEqual(self.status("wu-unique-claims"), "unmet")

    def test_wait_requires_unchanged_pending_callback_and_no_fake_completion(self):
        self.complete_original_artifacts()
        self.assertEqual(self.status("wu-wait-callback"), "waiting")
        self.write("wu-wait-callback", "waiting.json", '{"callback_id":"cb-17","status":"complete"}')
        self.assertEqual(self.status("wu-wait-callback"), "unmet")
        self.write("wu-wait-callback", "waiting.json", '{"callback_id":"cb-17","status":"waiting"}')
        self.write("wu-wait-callback", "callback.json", '{"callback_id":"cb-17","state":"complete"}')
        self.assertEqual(self.status("wu-wait-callback"), "unmet")

    def test_json_types_duplicate_keys_and_oversized_artifacts_are_invalid(self):
        self.write("wu-json-enable", "settings.json", '{"enabled":1,"retries":3,"tenant":"amber"}')
        self.assertEqual(self.status("wu-json-enable"), "unmet")
        self.write("wu-json-enable", "settings.json", '{"enabled":false,"enabled":true,"retries":3,"tenant":"amber"}')
        self.assertEqual(self.status("wu-json-enable"), "invalid")
        self.write("wu-text-normalize", "normalized.txt", "A" * 65537)
        self.assertEqual(self.status("wu-text-normalize"), "invalid")

    def test_symlink_artifact_and_forged_run_marker_are_rejected(self):
        target = self.root/"separate-private-fixture.txt"
        target.write_text("ALPHA\nBETA\n")
        (self.root/"wu-text-normalize/normalized.txt").symlink_to(target)
        self.assertEqual(self.status("wu-text-normalize"), "invalid")
        (self.root/"run.json").write_text('{"schema_hash":"forged"}')
        with self.assertRaises(ValueError):
            evaluate_run(self.root)

    def test_cli_evaluates_artifacts_without_running_actions_or_emitting_bodies(self):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("runner.py")),
                                 "evaluate", str(self.root)], capture_output=True, text=True, check=True)
        report = json.loads(result.stdout)
        self.assertEqual(report["counts"]["unmet"], 8)
        self.assertEqual(report["model_runs"], 0)
        self.assertNotIn("receipt payload v1", result.stdout)


if __name__ == "__main__":
    unittest.main()
