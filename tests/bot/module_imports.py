"""
What a module imports when it is imported, for the tests that check a module
stays inert: `test_access` (access.py imports neither discord nor the scraper)
and `test_intern_wiring` (app.py never imports the scraper at module scope).

One helper, so both guards read `from x import y` the same way: by x, the
module it imports from. Its alias names are y, which says nothing about x.
"""

import ast


def imported_at_module_scope(tree: ast.Module) -> set[str]:
    """The top-level package of every import that runs when the module is imported:
    `import a.b` and `from a.b import c` both give "a", inside a `try` or a class
    body too. Imports in a def body run only when it is called and are left out, as
    are relative imports, which name no package."""
    found = set()
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
        pending.extend(ast.iter_child_nodes(node))
    return found
