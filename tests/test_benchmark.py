import unittest

from realstop import benchmark as b


def case(i, session, family, split, source=None):
    return {'id': i, 'session': session, 'family': family, 'split': split,
            'source': source or i, 'snapshot': {'request': i}}


class BenchmarkTests(unittest.TestCase):
    def test_connected_leakage_and_duplicates_fail(self):
        cases = [case('a', 's1', 'f1', 'dev'), case('b', 's1', 'f2', 'dev'),
                 case('c', 's2', 'f2', 'test')]
        with self.assertRaises(b.BenchmarkError):
            b.validate_cases(cases)
        with self.assertRaises(b.BenchmarkError):
            b.validate_cases([case('a', 's1', 'f1', 'dev'), case('a', 's2', 'f2', 'test')])
        duplicate = case('b', 's2', 'f2', 'test'); duplicate['snapshot'] = {'request': 'a'}
        with self.assertRaises(b.BenchmarkError):
            b.validate_cases([case('a', 's1', 'f1', 'dev'), duplicate])

    def test_consumed_training_source_cannot_be_test(self):
        with self.assertRaises(b.BenchmarkError):
            b.validate_cases([case('new-id', 's', 'f', 'test', 'prior22')], {'prior22', 'curriculum32'})
        with self.assertRaises(b.BenchmarkError):
            b.validate_cases([case('new-id', 's', 'f', 'test', 'prior22')])
        x = case('a', 's', 'f', 'dev'); x['label'] = 'quit'
        with self.assertRaises(b.BenchmarkError):
            b.validate_cases([x])

    def test_freeze_then_score_counts_abstentions_and_unknowns(self):
        cases = [case(str(i), str(i), str(i), 'dev') for i in range(5)]
        gold = {'0': 'quit', '1': 'quit', '2': 'right_stop', '3': 'right_stop', '4': 'uncertain'}
        pred = {'0': {'action': 'continue', 'severe': False}, '1': {'action': 'abstain', 'severe': False},
                '2': {'action': 'continue', 'severe': True}, '3': {'action': 'abstain', 'severe': False},
                '4': {'action': 'abstain', 'severe': False}}
        candidate = {'model': 'frozen'}
        receipt = b.freeze_predictions(cases, candidate, pred, split='dev', gold_commitment=b.digest(gold))
        result = b.score(cases, candidate, pred, gold, receipt)
        self.assertEqual((result['recall'], result['false_continuation_rate']), (.5, .5))
        self.assertEqual((result['counts']['abstain_quit'], result['counts']['uncertain'], result['counts']['severe']), (1, 1, 1))
        with self.assertRaises(b.BenchmarkError):
            b.score(cases, {'model': 'changed'}, pred, gold, receipt)
        with self.assertRaises(b.BenchmarkError):
            b.score(cases, candidate, pred, {**gold, '0': 'right_stop'}, receipt)

    def test_infeasible_class_coverage_is_explicit(self):
        c = [case('a', 's', 'f', 'dev')]
        self.assertFalse(b.require_class_coverage(c, {'a': 'right_stop'}, 'dev')['feasible_binary_metrics'])
        with self.assertRaises(b.BenchmarkError):
            b.require_class_coverage(c, {}, 'dev')

    def test_group_bootstrap_constant_difference_and_missing_classes(self):
        cases = [case('a', 's1', 'f1', 'dev'), case('b', 's1', 'f2', 'dev'),
                 case('c', 's2', 'f2', 'dev'), case('d', 's3', 'f3', 'dev')]
        gold = {'a': 'quit', 'b': 'quit', 'c': 'quit', 'd': 'right_stop'}
        left = {i: {'action': 'continue'} for i in gold}
        right = {i: {'action': 'stop'} for i in gold}
        result = b.paired_component_bootstrap(cases, left, right, gold, seed=4, resamples=200)
        self.assertEqual(result['components'], 2)  # three session IDs are only two independent components
        self.assertEqual(result['metrics']['recall_delta']['interval_95'], [1.0, 1.0])
        self.assertEqual(result['metrics']['false_continuation_delta']['interval_95'], [1.0, 1.0])
        self.assertGreater(result['metrics']['recall_delta']['missing_class_replicates'], 0)
        self.assertGreater(result['metrics']['false_continuation_delta']['missing_class_replicates'], 0)
        self.assertEqual(result, b.paired_component_bootstrap(cases, left, right, gold, seed=4, resamples=200))
        with self.assertRaises(b.BenchmarkError):
            b.paired_component_bootstrap(cases, {k: v for k, v in left.items() if k != 'a'}, right, gold)
        with self.assertRaises(b.BenchmarkError):
            b.paired_component_bootstrap(cases, {**left, 'a': {'action': 'invented'}}, right, gold)

    def test_single_component_is_inadequate(self):
        cases = [case('a', 's', 'f', 'dev'), case('b', 's', 'f', 'dev')]
        gold = {'a': 'quit', 'b': 'right_stop'}
        predictions = {i: {'action': 'stop'} for i in gold}
        result = b.paired_component_bootstrap(cases, predictions, predictions, gold, resamples=20)
        self.assertTrue(result['metrics']['recall_delta']['inadequate_components'])
        self.assertIsNone(result['metrics']['recall_delta']['interval_95'])

    def test_paired_abstention_has_same_intervention_semantics(self):
        c = [case('a', 's', 'f', 'dev')]
        self.assertEqual(b.paired(c, {'a': {'action': 'abstain'}}, {'a': {'action': 'continue'}}, {'a': 'right_stop'})['left_only_correct'], 1)


if __name__ == '__main__':
    unittest.main()
