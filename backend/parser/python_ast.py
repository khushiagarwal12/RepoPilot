import ast
import os
from typing import List, Dict, Optional


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


def relative_posix_path(file_path: str, repo_root: Optional[str] = None) -> str:
    """Path of ``file_path`` relative to ``repo_root`` using ``/`` separators.

    Independent of the process working directory. When ``repo_root`` is omitted
    (single-file use) only the file name is returned.
    """
    if repo_root is None:
        return os.path.basename(file_path)
    return os.path.relpath(file_path, repo_root).replace(os.sep, "/")


def extract_definitions(file_path: str, repo_root: Optional[str] = None) -> List[Dict]:
    """Parse a single ``.py`` file and return top‑level definitions.

    Parameters
    ----------
    file_path:
        Absolute path to a Python source file.
    repo_root:
        Repository root used to compute the stored ``file`` path (relative,
        ``/``-separated). If omitted, only the file name is stored.

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
        d["file"] = relative_posix_path(file_path, repo_root)
    return visitor.definitions


def parse_repository(repo_root: str) -> List[Dict]:
    """Recursively walk *repo_root* and extract definitions from all ``.py`` files.

    The returned list is flat – each entry contains the file path relative to
    ``repo_root`` (``/``-separated) and metadata about the definition.
    """
    results: List[Dict] = []
    for dirpath, _dirnames, filenames in os.walk(repo_root):
        for fname in filenames:
            if fname.endswith(".py"):
                full_path = os.path.join(dirpath, fname)
                try:
                    results.extend(extract_definitions(full_path, repo_root))
                except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError, OSError):
                    # Skip files that cannot be read or parsed (syntax errors, non-UTF-8
                    # bytes, null bytes, pathological nesting) so one bad file cannot
                    # fail the ingestion of the whole repository.
                    continue
    return results
