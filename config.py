"""Settings read from the environment, in one place."""

import os
import secrets
import socket

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://durable:durable@localhost:5433/durable"
)

RUN_ID = os.environ.get("RUN_ID", "run1")
GOAL = os.environ.get("GOAL", "stand up a web service")

# Guard against a planner that never says DONE.
MAX_STEPS = 12

# Who this process is, written to runs.owner when it claims. The random suffix
# separates two workers that share a host and recycle a pid. Diagnostic only:
# epoch decides who may write, and this only says who to go and look at.
WORKER = os.environ.get(
    "WORKER", f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"
)
