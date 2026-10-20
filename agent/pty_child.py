"""Acquire a controlling terminal after spawn, then exec an argv without a shell.

Run only as a child on POSIX. Avoid preexec_fn in the multi-threaded Runner.
"""
import fcntl
import os
import sys
import termios

if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit(64)
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
