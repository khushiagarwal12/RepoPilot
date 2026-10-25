import ast
import os
from typing import List, Dict


class DefinitionVisitor(ast.NodeVisitor):
    """AST visitor that collects top‑level function and class definitions.

    The visitor attaches a ``parent`` attribute to each node while walking the
    tree so that we can discriminate between top‑level definitions and nested
    ones (e.g. methods inside a class).
    """

    def __init__(self) -> None:
        self.definitions: List[Dict] = []
        self._parent_stack: List[ast.AST] = []

    def generic_visit(self, node: ast.AST) -> None:
        # Attach parent reference for children
        for child in ast.iter_child_nodes(node):
            setattr(child, "parent", node)
        self._parent_stack.append(node)
        super().generic_visit(node)
        self._parent_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Record only if the parent is a module – i.e. top‑level functions
        if isinstance(getattr(node, "parent", None), ast.Module):
            self.definitions.append(
                {
                    "type": "function",
                    "name": node.name,
                    "start_line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                }
            )
        # Continue walking so that nested functions are visited (but not recorded)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Record only top‑level classes
        if isinstance(getattr(node, "parent", None), ast.Module):
            self.definitions.append(
                {
                    "type": "class",
                    "name": node.name,
                    "start_line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                }
            )
        # Walk inside the class to potentially capture nested classes (ignored)
        self.generic_visit(node)


def extract_definitions(file_path: str) -> List[Dict]:
    """Parse a single ``.py`` file and return top‑level definitions.

    Parameters
    ----------
    file_path:
        Absolute path to a Python source file.

    Returns
    -------
    List[Dict]
        A list of dictionaries, each describing a function or class.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=file_path, type_comments=True)
    # Attach a ``parent`` attribute to the root module for consistency
    setattr(tree, "parent", None)
    visitor = DefinitionVisitor()
    visitor.visit(tree)
    # Attach file information to each definition
    for d in visitor.definitions:
        d["file"] = os.path.relpath(file_path)
    return visitor.definitions


def parse_repository(repo_root: str) -> List[Dict]:
    """Recursively walk *repo_root* and extract definitions from all ``.py`` files.

    The returned list is flat – each entry contains the relative file path and
    metadata about the definition.
    """
    results: List[Dict] = []
    for dirpath, _dirnames, filenames in os.walk(repo_root):
        for fname in filenames:
            if fname.endswith(".py"):
                full_path = os.path.join(dirpath, fname)
                try:
                    results.extend(extract_definitions(full_path))
                except SyntaxError:
                    # Skip files that cannot be parsed (e.g. generated stubs)
                    continue
    return results
