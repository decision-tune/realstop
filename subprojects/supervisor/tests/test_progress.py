from dataclasses import replace
from pathlib import Path
import sys
import unittest

from realstop.supervisor.progress import Criterion, Receipt, Contract, assess

class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.a=Criterion('a','site','rev1','tests','local'); self.b=Criterion('b','site','rev1','build','local')
        self.r=Receipt('receipt1',self.a,'pass')
        self.c=Contract((self.a,self.b),2,permission='allowed',attempt_limit=3)
    def test_units_receipts_and_repeat_do_not_fabricate_progress(self):
        before=assess(self.c); one=assess(replace(self.c,receipts=(self.r,self.r)),before)
        self.assertEqual((one.completion,one.new_units),('unmet',('a',)))
        two=assess(replace(self.c,receipts=(self.r,Receipt('same-unit',self.a,'pass'))),one)
        self.assertEqual(two.new_units,()); self.assertEqual(len(two.accepted),1)
        with self.assertRaises(ValueError): replace(self.c,receipts=(self.r,replace(self.r,result='fail')))
    def test_optimistic_text_never_changes_progress(self):
        previous=assess(replace(self.c,receipts=(self.r,)))
        for _ in ('Nearly done','Surely finished','I will complete it'):
            result=assess(replace(self.c,receipts=(self.r,),attempts_without_progress=3),previous)
            self.assertEqual(result.new_units,()); self.assertEqual(result.stop_reason,'attempt_limit')
    def test_callback_and_priority(self):
        waiting=assess(replace(self.c,pending_callbacks=1,attempts_without_progress=3))
        self.assertEqual((waiting.completion,waiting.stop_reason),('unmet','waiting_callback'))
        for changes, reason in (({'user_stop':True,'budget_exhausted':True},'user_stop'),
                                ({'budget_exhausted':True},'budget_exhausted'),
                                ({'permission':'denied'},'permission_denied'),
                                ({'permission':'unknown'},'permission_unknown'),
                                ({'scope':'cancelled'},'scope_cancelled')):
            self.assertEqual(assess(replace(self.c,pending_callbacks=1,**changes)).stop_reason,reason)
    def test_completion_and_budget_are_separate(self):
        c=replace(self.c,receipts=(self.r,Receipt('r2',self.b,'pass')),budget_exhausted=True)
        p=assess(c); self.assertEqual((p.completion,p.stop_reason),('satisfied','budget_exhausted'))
    def test_revocation_changed_revision_and_requirements_invalidate(self):
        old=assess(replace(self.c,receipts=(self.r,)))
        revoked=assess(replace(self.c,receipts=(self.r,),revoked_ids=('receipt1',)),old)
        self.assertEqual(revoked.accepted,()); self.assertEqual(revoked.new_units,())
        changed=replace(self.a,revision='rev2')
        stale=assess(replace(self.c,criteria=(changed,self.b),receipts=(self.r,)),old)
        self.assertEqual(stale.accepted,()); self.assertEqual(stale.completion,'unmet')
        altered=replace(self.a,check='integration-tests')
        self.assertEqual(assess(replace(self.c,criteria=(altered,self.b),receipts=(self.r,))).accepted,())
    def test_receipt_for_different_environment_does_not_satisfy(self):
        wrong=Receipt('different-env',replace(self.a,environment='staging'),'pass')
        result=assess(replace(self.c,receipts=(wrong,)))
        self.assertEqual(result.accepted,())
        self.assertEqual(result.completion,'unmet')
        correct=assess(replace(self.c,receipts=(wrong,self.r)))
        self.assertEqual(correct.accepted,(self.a,))
        with self.assertRaises(ValueError): replace(self.a,environment='')

    def test_conflicting_results_do_not_prove_completion(self):
        p=assess(replace(self.c,receipts=(self.r,Receipt('different',self.a,'fail'))))
        self.assertEqual(p.accepted,())
    def test_bounds_and_bad_flags(self):
        for changes in ({'attempt_limit':0},{'attempt_limit':1001},{'user_stop':1},{'target_count':3}, {'criteria':(self.a,self.a)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): replace(self.c,**changes)

if __name__=='__main__': unittest.main()
