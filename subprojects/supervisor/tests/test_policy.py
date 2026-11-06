"""Policy boundary tests: trusted facts in, conservative Stop decision out."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest

from realstop.supervisor.policy import (Acceptance, Obligation, Receipt, NextAction, Permission,
                    Snapshot, evaluate_policy, validate_confirmed_action,
                    build_residual_packet)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.test = Acceptance("site", "rev-b", "tests", "local")
        self.publish = Acceptance("site", "rev-b", "published", "production")
        self.obligation = Obligation("o", "owner", (self.test,))
        self.action = NextAction("run-tests", "o", self.test, "action-source")
        self.snapshot = Snapshot("task", "version-1", "reply-hash", "progress-1",
            obligations=(self.obligation,), actions=(self.action,),
            permissions=(Permission("run-tests", "allowed", "permission-source"),),
            coverage="supported", feasibility="actionable",
            authority_complete=True, scope_resolved=True)

    def decide(self, **changes):
        return evaluate_policy(replace(self.snapshot, **changes))

    def confirm(self, snapshot=None, **changes):
        args = dict(confirmed_snapshot="version-1", confirmed_reply_digest="reply-hash",
                    action_id="run-tests", source_ids=("owner", "action-source", "permission-source"),
                    native_confirmed=True)
        args.update(changes)
        return validate_confirmed_action(snapshot or self.snapshot, **args)

    def test_identical_successful_tools_do_not_prove_different_goals(self):
        receipt = Receipt("o", self.test, "pass", "test-result")
        self.assertEqual(self.decide(receipts=(receipt,)).completion, "met")
        publish_goal = replace(self.obligation, acceptance=(self.publish,))
        result = self.decide(obligations=(publish_goal,), receipts=(receipt,))
        self.assertEqual(result.completion, "unmet")
        self.assertNotEqual(result.reason, "acceptance_met")
        existence = Receipt("o", replace(self.test, check="file-exists"), "pass", "exists")
        self.assertEqual(self.decide(receipts=(existence,)).completion, "unmet")

    def test_receipts_bind_all_acceptance_dimensions_and_obligation(self):
        for field, value in (("revision", "rev-a"), ("artifact", "other"),
                             ("check", "build"), ("environment", "production")):
            with self.subTest(field=field):
                r = Receipt("o", replace(self.test, **{field:value}), "pass", "receipt")
                self.assertEqual(self.decide(receipts=(r,)).completion, "unmet")
        r = Receipt("other-obligation", self.test, "pass", "receipt")
        self.assertEqual(self.decide(receipts=(r,)).completion, "unmet")

    def test_partial_conflicting_and_empty_acceptance_never_complete(self):
        passed = Receipt("o", self.test, "pass", "pass-source")
        failed = Receipt("o", self.test, "fail", "fail-source")
        self.assertEqual(self.decide(receipts=(passed, failed)).completion, "unmet")
        goal = replace(self.obligation, acceptance=(self.test, self.publish))
        snap = replace(self.snapshot, obligations=(goal,), receipts=(passed,))
        self.assertEqual(evaluate_policy(snap).completion, "unmet")
        self.assertIsNone(self.confirm(snap))  # Already-met subcheck is not the next action.
        self.assertEqual(self.decide(obligations=(replace(self.obligation, acceptance=()),)).completion, "unmet")

    def test_stop_budget_denied_wait_and_block_do_not_intervene(self):
        for changes, reason in (({"user_stop":True, "budget_exhausted":True}, "user_stop"),
                                ({"budget_exhausted":True}, "budget_exhausted"),
                                ({"permissions":(Permission("run-tests", "denied", "denial"),)}, "permission_denied"),
                                ({"feasibility":"waiting"}, "waiting"),
                                ({"feasibility":"blocked"}, "blocked")):
            with self.subTest(reason=reason):
                result = self.decide(**changes)
                self.assertEqual((result.disposition, result.reason), ("allow", reason))
                self.assertIsNone(self.confirm(replace(self.snapshot, **changes)))

    def test_cancellation_and_supersession_do_not_leave_residual_work(self):
        for status in ("cancelled", "superseded"):
            result = self.decide(obligations=(replace(self.obligation, status=status),))
            self.assertEqual((result.disposition, result.completion), ("allow", "unknown"))

    def test_unknown_authority_scope_coverage_and_missing_permission_abstain(self):
        passed = Receipt("o", self.test, "pass", "receipt")
        for changes in ({"authority_complete":False}, {"scope_resolved":False},
                        {"coverage":"partial"}, {"coverage":"unsupported"},
                        {"permissions":()}, {"obligations":(replace(self.obligation, status="unresolved"),)}):
            with self.subTest(changes=changes):
                self.assertEqual(self.decide(**changes).disposition, "abstain")
        self.assertEqual(self.decide(coverage="partial", receipts=(passed,)).completion, "unknown")

    def test_permissions_are_scoped_to_unmet_acceptance_actions(self):
        publish = NextAction("publish", "o", self.publish, "publish-action")
        goal = replace(self.obligation, acceptance=(self.test, self.publish))
        snap = replace(self.snapshot, obligations=(goal,), actions=(self.action, publish),
                       permissions=self.snapshot.permissions +
                       (Permission("publish", "denied", "publish-denial"),))
        decision = evaluate_policy(snap)
        self.assertEqual((decision.disposition, decision.authorization),
                         ("needs_native_review", "allowed"))
        self.assertIsNotNone(self.confirm(snap))
        self.assertIsNone(self.confirm(snap, action_id="publish",
                                      source_ids=("owner", "publish-action", "publish-denial")))
        # The same receipts with no allowed remaining action must not review.
        denied_only = replace(snap, receipts=(Receipt("o", self.test, "pass", "tests-passed"),))
        self.assertEqual(evaluate_policy(denied_only).reason, "permission_denied")
        unknown_only = replace(denied_only, permissions=self.snapshot.permissions)
        self.assertEqual(evaluate_policy(unknown_only).disposition, "abstain")
        # Unknown unrelated actions and already-met criteria do not mask tests.
        unrelated = replace(publish, obligation_id="another-task")
        for ignored in (unrelated, publish):
            with self.subTest(action=ignored.obligation_id):
                receipts = ((Receipt("o", self.publish, "pass", "published"),)
                            if ignored is publish else ())
                mixed = replace(snap, actions=(self.action, ignored),
                                permissions=self.snapshot.permissions, receipts=receipts)
                self.assertEqual(evaluate_policy(mixed).disposition, "needs_native_review")
                self.assertIsNotNone(self.confirm(mixed))
        self.assertEqual(evaluate_policy(replace(denied_only, scope_resolved=False)).disposition,
                         "abstain")

    def test_native_confirmation_requires_current_binding_and_sources(self):
        self.assertIsNotNone(self.confirm())
        for changes in ({"native_confirmed":False}, {"native_confirmed":"yes"},
                        {"confirmed_snapshot":"stale"}, {"confirmed_reply_digest":"stale"},
                        {"action_id":"invented-command"}, {"source_ids":("invented-source",)},
                        {"source_ids":("owner", "action-source")}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.confirm(**changes))
        self.assertIsNone(self.confirm(replace(self.snapshot, permissions=())))

    def test_retry_requires_both_material_progress_and_changed_relevant_evidence(self):
        claim = self.confirm()
        used = replace(self.snapshot, interventions=(claim,))
        self.assertIsNone(self.confirm(used))
        self.assertIsNone(self.confirm(replace(used, version="new", reply_digest="new"),
                                       confirmed_snapshot="new", confirmed_reply_digest="new"))
        self.assertIsNone(self.confirm(replace(used, progress_id="progress-2")))
        failed = Receipt("o", self.test, "fail", "failure")
        self.assertIsNone(self.confirm(replace(used, receipts=(failed,))))
        progressed = replace(used, receipts=(failed,), progress_id="progress-2")
        self.assertIsNotNone(self.confirm(progressed))
        again = self.confirm(progressed)
        duplicated = replace(progressed, interventions=(claim, again),
                             receipts=(failed, replace(failed, source_id="duplicate")), progress_id="progress-3")
        self.assertIsNone(self.confirm(duplicated))
        goal = replace(self.obligation, acceptance=(self.test, self.publish))
        multiple = replace(self.snapshot, obligations=(goal,))
        original_claim = self.confirm(multiple)
        for acceptance in ((self.publish, self.test), (self.test, self.publish, self.test)):
            with self.subTest(acceptance=acceptance):
                same_facts = replace(multiple, obligations=(replace(goal, acceptance=acceptance),),
                                     progress_id="progress-2", interventions=(original_claim,))
                self.assertIsNone(self.confirm(same_facts))

    def test_packet_has_bounded_references_and_no_inferred_payload_authority(self):
        packet = build_residual_packet(self.snapshot)
        self.assertEqual(packet["snapshot"], "version-1")
        self.assertEqual(packet["decision"]["disposition"], "needs_native_review")
        self.assertIsNone(build_residual_packet(replace(self.snapshot, coverage="partial")))
        receipts = tuple(Receipt("o", self.test, "fail", "source-"+str(i)) for i in range(40))
        result = self.decide(receipts=receipts)
        self.assertEqual(result.disposition, "abstain")
        self.assertLessEqual(len(result.source_ids), 32)
        with self.assertRaises(ValueError):
            evaluate_policy({"role":"user", "authorized":True})

    def test_invalid_typed_facts_rejected(self):
        for changes in ({"user_stop":"false"}, {"obligations":[self.obligation]},
                        {"actions":(self.action, self.action)}, {"coverage":"complete"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.snapshot, **changes)


if __name__ == "__main__":
    unittest.main()
