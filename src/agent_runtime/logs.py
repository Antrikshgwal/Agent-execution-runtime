"""Log format.

One event per line, machine-greppable, so a crash test can assert on the output
rather than on a debugger.
"""

from datetime import datetime, timezone
from typing import Any


def log(event: str, **fields: Any) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    stamp = stamp.replace("+00:00", "Z")
    pairs = "  ".join(f"{key}={value}" for key, value in fields.items())
    print(f"{stamp} INFO  {event:<10} {pairs}", flush=True)


def banner(text: str) -> None:
    print(f"================ {text} ================", flush=True)


def line(text: str) -> None:
    """A detail line inside a banner block."""
    print(f"  {text}", flush=True)
