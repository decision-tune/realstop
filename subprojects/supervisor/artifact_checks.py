"""Bounded local SHA-256 observations, not authorization or durable proof."""
from dataclasses import dataclass
import hashlib
import os
import re
import stat
import time
import uuid

from contracts import CriterionDeclaration, ContractError, _id, _path

HARD_MAX_BYTES = 16 * 1024 * 1024
CHUNK = 65536


@dataclass(frozen=True)
class ArtifactObservation:
    observation_id: str
    criterion: CriterionDeclaration
    environment_id: str
    status: str
    actual_sha256: str | None
    observed_at_ns: int


def _identity(st):
    return (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)


def _open_root(root):
    if not isinstance(root,str) or not root.startswith('/') or '\x00' in root:
        raise ValueError('invalid_root')
    parts=root.split('/')[1:]
    if any(part in ('','.','..') for part in parts): raise ValueError('invalid_root')
    fd=os.open('/',os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child=os.open(part,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd=child
        return fd
    except BaseException:
        os.close(fd); raise


def _open_target(root, relative):
    try: fd=_open_root(root)
    except FileNotFoundError as exc: raise ValueError('invalid_root') from exc
    try:
        parts=relative.split('/')
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd=child
        return os.open(parts[-1],os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,dir_fd=fd)
    finally: os.close(fd)


def check(root, criterion, environment_id, *, max_bytes=HARD_MAX_BYTES):
    """Read one regular file without following symlinks; return point-in-time metadata."""
    def result(status, actual=None):
        return ArtifactObservation(uuid.uuid4().hex,criterion,environment_id,status,actual,time.time_ns())
    try:
        if (not isinstance(criterion,CriterionDeclaration) or
            not isinstance(criterion.stable_id,str) or not re.fullmatch(r'[0-9a-f]{64}',criterion.stable_id) or
            not isinstance(criterion.expected_sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',criterion.expected_sha256) or
            criterion.check!='sha256'):
            return result('unverifiable')
        _id(criterion.id); _id(criterion.environment_id); _id(environment_id)
        relative=_path(criterion.artifact_relative_path)
        if criterion.environment_id!=environment_id: return result('unverifiable')
        if type(max_bytes) is not int or not 0<max_bytes<=HARD_MAX_BYTES: return result('unverifiable')
        if not all(hasattr(os,x) for x in ('O_NOFOLLOW','O_DIRECTORY','O_NONBLOCK')) or os.open not in os.supports_dir_fd:
            return result('unverifiable')
        fd=_open_target(root,relative)
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size>max_bytes: return result('unverifiable')
            digest=hashlib.sha256(); count=0
            while True:
                chunk=os.read(fd,min(CHUNK,max_bytes+1-count))
                if not chunk: break
                count+=len(chunk)
                if count>max_bytes: return result('unverifiable')
                digest.update(chunk)
            after=os.fstat(fd)
            if _identity(before)!=_identity(after) or count!=after.st_size: return result('unverifiable')
            # Re-resolve root and every component to detect replacement while reading.
            try: recheck=_open_target(root,relative)
            except FileNotFoundError: return result('unverifiable')
            try:
                current=os.fstat(recheck)
                if not stat.S_ISREG(current.st_mode) or _identity(current)!=_identity(after): return result('unverifiable')
            finally: os.close(recheck)
            actual=digest.hexdigest()
            return result('match' if actual==criterion.expected_sha256 else 'mismatch',actual)
        finally: os.close(fd)
    except FileNotFoundError:
        return result('missing')
    except (OSError,ValueError,TypeError,ContractError,UnicodeError):
        return result('unverifiable')
