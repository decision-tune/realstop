#!/usr/bin/env python3
"""Run a command on a real pseudo-terminal, typing PTY_INPUT ahead; print its output, exit with its status.
Used to drive `realstop learn` (which refuses without a TTY) the way a person would, not through an env override.
  PTY_INPUT=$'y\\nn\\n' python3 tests/pty_run.py CMD ARGS..."""
import os
import pty
import sys

pid, fd = pty.fork()
if pid == 0:
    os.execv(sys.argv[1], sys.argv[1:])
os.write(fd, os.environ.get("PTY_INPUT", "").encode() + b"\x04")  # typed ahead, then Ctrl-D: the prompt reads end-of-file as quit
out = b""
while True:
    try:
        chunk = os.read(fd, 65536)
    except OSError:
        break
    if not chunk:
        break
    out += chunk
_, status = os.waitpid(pid, 0)
sys.stdout.write(out.decode("utf-8", "replace").replace("\r\n", "\n"))
sys.exit(os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8))
