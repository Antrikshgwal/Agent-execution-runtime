"""A durable agent runtime.

An agent loop that survives `kill -9` at any point and resumes without repeating
an external action. Two lines of the loop touch the outside world, and both
follow the same pattern: commit an intent row, act, commit a confirm.

    runtime     the loop
    journal     decisions, replay, and what the planner cost
    executor    one tool call, intent-first
    recovery    what a crash left behind
    fencing     who owns the run, and what happens to everyone else
    tools       the registry the loop dispatches through
"""

__all__ = ["__version__"]

__version__ = "0.7.0"
