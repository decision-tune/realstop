import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import artifact_checks as a
from contracts import CriterionDeclaration

class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name).resolve()
        (self.root/'out').mkdir(); (self.root/'out'/'file').write_bytes(b'hello')
        self.c=CriterionDeclaration('c','a'*64,'out/file',hashlib.sha256(b'hello').hexdigest(),'sha256','local')
    def tearDown(self): self.tmp.cleanup()
    def check(self,c=None,**kw): return a.check(str(self.root),c or self.c,'local',**kw)
    def test_match_mismatch_missing_and_bindings(self):
        result=self.check();self.assertEqual(result.status,'match');self.assertEqual(result.actual_sha256,self.c.expected_sha256)
        self.assertEqual((result.criterion,result.environment_id),(self.c,'local'))
        (self.root/'out'/'file').write_bytes(b'other')
        self.assertEqual(self.check().status,'mismatch')
        (self.root/'out'/'file').unlink();self.assertEqual(self.check().status,'missing')
    def test_wrong_environment_malformed_and_limits(self):
        self.assertEqual(a.check(str(self.root),self.c,'prod').status,'unverifiable')
        for c in (replace(self.c,expected_sha256='bad'),replace(self.c,stable_id='bad'),replace(self.c,artifact_relative_path='../escape'),replace(self.c,check='shell')):
            self.assertEqual(self.check(c).status,'unverifiable')
        self.assertEqual(self.check(max_bytes=4).status,'unverifiable')
        self.assertEqual(self.check(max_bytes=a.HARD_MAX_BYTES+1).status,'unverifiable')
    def test_symlink_leaf_and_parent_refused(self):
        (self.root/'out'/'link').symlink_to(self.root/'out'/'file')
        self.assertEqual(self.check(replace(self.c,artifact_relative_path='out/link')).status,'unverifiable')
        (self.root/'alias').symlink_to(self.root/'out',target_is_directory=True)
        self.assertEqual(self.check(replace(self.c,artifact_relative_path='alias/file')).status,'unverifiable')
        self.assertEqual(a.check(str(self.root/'alias'),replace(self.c,artifact_relative_path='file'),'local').status,'unverifiable')
    def test_directory_and_fifo_are_not_read(self):
        self.assertEqual(self.check(replace(self.c,artifact_relative_path='out')).status,'unverifiable')
        os.mkfifo(self.root/'out'/'fifo')
        self.assertEqual(self.check(replace(self.c,artifact_relative_path='out/fifo')).status,'unverifiable')
    def test_mutation_during_read_is_unverifiable(self):
        read=os.read; changed=[False]
        def mutate(fd,n):
            result=read(fd,n)
            if result and not changed[0]:
                changed[0]=True; (self.root/'out'/'file').write_bytes(b'different contents')
            return result
        with patch.object(a.os,'read',side_effect=mutate): self.assertEqual(self.check().status,'unverifiable')
    def test_replacement_during_read_is_unverifiable(self):
        read=os.read; changed=[False]
        def mutate(fd,n):
            result=read(fd,n)
            if result and not changed[0]:
                changed[0]=True; (self.root/'out'/'file').unlink(); (self.root/'out'/'file').write_bytes(b'hello')
            return result
        with patch.object(a.os,'read',side_effect=mutate): self.assertEqual(self.check().status,'unverifiable')

if __name__=='__main__': unittest.main()
