"""Settings read from the environment, in one place."""

import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://durable:durable@localhost:5433/durable"
)

RUN_ID = os.environ.get("RUN_ID", "run1")
GOAL = os.environ.get("GOAL", "stand up a web service")

# Guard against a planner that never says DONE.
MAX_STEPS = 12
