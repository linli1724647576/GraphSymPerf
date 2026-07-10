"""OS process-group termination helper for hard deadlines."""

from __future__ import annotations

import os
import signal


def terminate_process_group(pid: int) -> None:
    os.killpg(os.getpgid(pid), signal.SIGKILL)
