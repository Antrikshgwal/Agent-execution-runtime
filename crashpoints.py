"""Hard-kill injection.

CRASH_AT names a point in the step where the process dies, and CRASH_SEQ which
step it dies on, so a crash can be aimed at the first step or partway through a
run. Every call site sits on the normal path, so the crashing run and the
production run execute the same code. os._exit(1) skips stack unwinding, finally
blocks, buffer flushes, and connection teardown, which is what separates it from
an exception or sys.exit and what makes it a model of kill -9.
"""

import os
import sys

CRASH_AT = os.environ.get("CRASH_AT")
CRASH_SEQ = int(os.environ.get("CRASH_SEQ", "0"))

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
# untouched no matter which point is armed: a crash injected into the recovery
# path would destroy the state the test exists to inspect.
_step: int | None = None  # pylint: disable=invalid-name


def at_step(seq: int) -> None:
    """Arm the crash points for this step. Called once per loop iteration."""
    global _step  # pylint: disable=global-statement
    _step = seq


def crash(point: str) -> None:
    """Die here if this is the armed point on the armed step."""
    if CRASH_AT != point or _step != CRASH_SEQ:
        return
    print(f"!!! CRASH_AT={point} seq={_step} -- os._exit(1)", flush=True)
    sys.stdout.flush()
    os._exit(1)
