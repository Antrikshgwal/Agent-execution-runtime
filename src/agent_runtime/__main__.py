"""Entry point for `python -m agent_runtime`.

The crash tests spawn the runtime this way, so the module they kill is the one a
person runs.
"""

from agent_runtime.runtime import run_cli

if __name__ == "__main__":
    run_cli()
