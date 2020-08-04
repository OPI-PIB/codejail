"""Subprocess helpers for CodeJail."""

import functools
import logging
import os
import resource
import subprocess
import threading
import time
import requests
import json

try:
    from lms.envs.common import NAVOICA_SANDBOX
    from lms.envs.common import NAVOICA_SANDBOX_URL
except ImportError:
    NAVOICA_SANDBOX = False

log = logging.getLogger("codejail")

def run_subprocess(
    cmd, stdin=None, cwd=None, env=None, rlimits=None, realtime=None,
    slug=None,
):
    """
    A helper to make a limited subprocess.

    `cmd`, `cwd`, and `env` are exactly as `subprocess.Popen` expects.

    `stdin` is the data to write to the stdin of the subprocess.

    `rlimits` is a list of tuples, the arguments to pass to
    `resource.setrlimit` to set limits on the process.

    `realtime` is the number of seconds to limit the execution of the process.

    `slug` is a short identifier for use in log messages.

    This function waits until the process has finished executing before
    returning.

    Returns a tuple of three values: the exit status code of the process, and
    the stdout and stderr of the process, as strings.

    """

    if NAVOICA_SANDBOX:
        url = NAVOICA_SANDBOX_URL
        r = requests.post(url, json=stdin)
        stdout = json.dumps(r.json)
        stderr = r.status_code
        return_code = r.status_code

        # Difference between popen and http requests
        if return_code == 200:
            return_code = 0

    else:
        subproc = subprocess.Popen(
            cmd, cwd=cwd, env=env,
            preexec_fn=functools.partial(set_process_limits, rlimits or ()),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )

        if slug:
            log.info("Executed jailed code %s in %s, with PID %s", slug, cwd, subproc.pid)

        # Start the time killer thread.
        if realtime:
            killer = ProcessKillerThread(subproc, limit=realtime)
            killer.start()

        stdout, stderr = subproc.communicate(stdin)
        return_code = subproc.returncode
    return return_code, stdout, stderr


def set_process_limits(rlimits):       # pragma: no cover
    """
    Set limits on this process, to be used first in a child process.
    """
    # Set a new session id so that this process and all its children will be
    # in a new process group, so we can kill them all later if we need to.
    os.setsid()

    for limit, value in rlimits:
        resource.setrlimit(limit, value)


class ProcessKillerThread(threading.Thread):
    """
    A thread to kill a process after a given time limit.
    """
    def __init__(self, subproc, limit):
        super(ProcessKillerThread, self).__init__()
        self.subproc = subproc
        self.limit = limit

    def run(self):
        start = time.time()
        while (time.time() - start) < self.limit:
            time.sleep(.25)
            if self.subproc.poll() is not None:
                # Process ended, no need for us any more.
                return

        if self.subproc.poll() is None:
            # Can't use subproc.kill because we launched the subproc with sudo.
            pgid = os.getpgid(self.subproc.pid)
            log.warning(
                "Killing process %r (group %r), ran too long: %.1fs",
                self.subproc.pid, pgid, time.time() - start
            )
            subprocess.call(["sudo", "pkill", "-9", "-g", str(pgid)])
