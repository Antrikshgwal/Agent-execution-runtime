"""Hard-kill injection.

CRASH_AT names a point in the step where the process dies. Every call site sits
on the normal path, so the crashing run and the production run execute the same
code. os._exit(1) skips stack unwinding, finally blocks, buffer flushes, and
connection teardown, which is what separates it from an exception or sys.exit
and what makes it a model of kill -9.
"""

import os
import sys

CRASH_AT = os.environ.get("CRASH_AT")

POINTS = (
    "before_decide",
    "after_decide_before_journal",
    "after_decide",
    "before_intent",
    "after_intent",
    "after_call",
    "after_confirm",
)


def crash(point: str) -> None:
    if CRASH_AT != point:
        return
    print(f"!!! CRASH_AT={point} -- os._exit(1)", flush=True)
    sys.stdout.flush()
    os._exit(1)
