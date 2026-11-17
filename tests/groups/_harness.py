"""Run a group file by path and count how many assert statements executed.

Usage: _harness.py GROUP.py [args...]
The count is written to $OPENSTICK_CHECK_COUNT_FILE (set by tests/run-group.py).
A group that executes zero asserts is reported as vacuous by the runner.
"""
import ast
import os
import sys

path = sys.argv[1]
sys.argv = [path] + sys.argv[2:]
with open(path, encoding="utf-8") as handle:
    source = handle.read()
tree = ast.parse(source, path)
counter = [0]


def _count():
    counter[0] += 1


class _CountAsserts(ast.NodeTransformer):
    def visit_Assert(self, node):
        self.generic_visit(node)
        call = ast.Expr(ast.Call(ast.Name("__check_count__", ast.Load()), [], []))
        return [ast.copy_location(call, node), node]


tree = ast.fix_missing_locations(_CountAsserts().visit(tree))
namespace = {"__name__": "__main__", "__file__": path, "__check_count__": _count}
try:
    exec(compile(tree, path, "exec"), namespace)
finally:
    target = os.environ.get("OPENSTICK_CHECK_COUNT_FILE")
    if target:
        with open(target, "w", encoding="ascii") as out:
            out.write(str(counter[0]))
