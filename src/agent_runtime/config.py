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

# How often a worker refreshes runs.claimed_at while it holds a run. Without
# this the column records when the claim happened, which cannot distinguish a
# worker that is busy from one that died an hour ago.
HEARTBEAT_SECONDS = float(os.environ.get("HEARTBEAT_SECONDS", "3"))

# How long claimed_at may go unrefreshed before the supervisor treats the run as
# abandoned. Several heartbeats, so one slow beat does not cost a worker its run.
# It bounds how long a crashed run waits to be picked up, and nothing else: the
# claim itself never waits on it, or a restarting worker could not take back the
# run it just crashed out of.
LEASE_SECONDS = float(os.environ.get("LEASE_SECONDS", "12"))

# How often the supervisor looks for runs whose heartbeat stopped.
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "3"))
