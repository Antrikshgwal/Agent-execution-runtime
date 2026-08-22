"""The import graph runs one way, and this asserts it still does.

ARCHITECTURE.md claims a layering: recovery reaches for executor and journal,
both reach for tools and fencing, and nothing reaches back. A claim a document
makes and nothing checks is a claim that rots, so the layers live here as data
and every module's imports are read out of its syntax tree.

A module may import from a strictly lower layer. Same-layer and upward imports
are what this exists to catch, along with the runtime reaching into the tests or
the mocks.

    python tools/check_imports.py
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "agent_runtime"

# Lower numbers are further from the loop. A module may import any layer below
# its own, and nothing at or above it.
LAYERS: dict[str, int] = {
    "logs": 0,
    "config": 0,
    "crashpoints": 0,
    "tools": 1,
    "fencing": 1,
    "planner": 1,
    "demo_tools": 2,
    "journal": 2,
    "executor": 2,
    "recovery": 3,
    "runtime": 4,
    "__init__": 5,
    "__main__": 5,
}

FORBIDDEN_ROOTS = ("tests", "mock", "harness")


def imported_modules(tree: ast.AST) -> set[str]:
    """Every `agent_runtime.x` this file pulls in, by bare module name.

    Covers both spellings the package uses: `from agent_runtime import a, b` and
    `from agent_runtime.a import name`.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "agent_runtime":
                found.update(alias.name for alias in node.names)
            elif node.module.startswith("agent_runtime."):
                found.add(node.module.split(".", 1)[1].split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("agent_runtime."):
                    found.add(alias.name.split(".", 1)[1].split(".")[0])
    return found


def outside_imports(tree: ast.AST) -> set[str]:
    """Imports of things the shipped package has no business knowing about."""
    found: set[str] = set()
    for node in ast.walk(tree):
        roots: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            roots.append(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            roots.extend(alias.name.split(".")[0] for alias in node.names)
        found.update(root for root in roots if root in FORBIDDEN_ROOTS)
    return found


def main() -> int:
    """Read every module's imports and report each one that runs upward."""
    failures: list[str] = []
    seen: set[str] = set()

    for path in sorted(PACKAGE.glob("*.py")):
        name = path.stem
        seen.add(name)
        if name not in LAYERS:
            failures.append(f"{path.name}: no layer assigned; add it to LAYERS")
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        layer = LAYERS[name]

        for target in sorted(imported_modules(tree)):
            if target not in LAYERS:
                failures.append(f"{name} imports unknown module {target}")
            elif LAYERS[target] >= layer:
                failures.append(
                    f"{name} (layer {layer}) imports {target} (layer {LAYERS[target]});"
                    " imports must run downward only"
                )

        for target in sorted(outside_imports(tree)):
            failures.append(f"{name} imports {target}, which the package must not depend on")

    for missing in sorted(set(LAYERS) - seen):
        failures.append(f"LAYERS names {missing}, which no longer exists")

    for failure in failures:
        print(f"FAIL  {failure}")

    if failures:
        print(f"\n{len(failures)} problem(s) in the import graph")
        return 1

    print(f"import graph OK  ({len(seen)} modules, {max(LAYERS.values()) + 1} layers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
