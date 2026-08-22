"""Fault injection at named points in a step.

Two faults share one set of points, because they model the two ways a worker
stops being useful without saying so.

CRASH_AT names the point where the process dies. os._exit(1) skips stack
unwinding, finally blocks, buffer flushes, and connection teardown, which is what
separates it from an exception or sys.exit and what makes it a model of kill -9.

STALL_AT names the point where the process sleeps for STALL_MS instead. The
worker stays alive and stops running, which is the state fencing exists for: a
stalled owner another worker concludes is dead. The spec reaches that state with
SIGSTOP and SIGCONT, which Windows does not have, and an injected sleep gets
there on any platform. It is also deterministic, which a race between two live
processes is not.

CRASH_SEQ picks which step either fault lands on, so it can be aimed at the first
step or partway through a run.

Every call site sits on the normal path, so an injected run and a production run
execute the same code.
"""

import os
import sys
import time

CRASH_AT = os.environ.get("CRASH_AT")
STALL_AT = os.environ.get("STALL_AT")
CRASH_SEQ = int(os.environ.get("CRASH_SEQ", "0"))
STALL_MS = int(os.environ.get("STALL_MS", "8000"))

POINTS = (
    "before_decide",
    "after_decide_before_journal",
    "after_decide",
    "before_intent",
    "after_intent",
    "after_call",
    "after_confirm",
)

# Which step the loop is on. None until the loop starts, so recovery finishes
# untouched no matter which point is armed: a fault injected into the recovery
# path would destroy the state the test exists to inspect.
_step: int | None = None  # pylint: disable=invalid-name


def at_step(seq: int) -> None:
    """Arm the injection points for this step. Called once per loop iteration."""
    global _step  # pylint: disable=global-statement
    _step = seq


def interrupt(point: str) -> None:
    """Stall here, or die here, if this is the armed point on the armed step.

    Stalling is checked first so a single run can do both: sleep long enough to
    be superseded, then carry on into whatever the test wants to watch it try.
    """
    if _step != CRASH_SEQ:
        return

    if STALL_AT == point:
        print(f"... STALL_AT={point} seq={_step} -- sleeping {STALL_MS}ms", flush=True)
        sys.stdout.flush()
        time.sleep(STALL_MS / 1000)
        print(f"... STALL_AT={point} seq={_step} -- awake", flush=True)
        sys.stdout.flush()

    if CRASH_AT == point:
        print(f"!!! CRASH_AT={point} seq={_step} -- os._exit(1)", flush=True)
        sys.stdout.flush()
        os._exit(1)
