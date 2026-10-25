import os
import tempfile
import shutil
from pathlib import Path
import pytest

from backend.parser.python_ast import parse_repository, extract_definitions


def write_file(base_dir: Path, relative_path: str, content: str) -> Path:
    target = base_dir / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


@pytest.fixture
def temp_repo():
    temp_dir = Path(tempfile.mkdtemp())
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_top_level_extraction_and_line_numbers(temp_repo):
    # line 1: # module docstring
    # line 2: (blank)
    # line 3: def standalone_func():
    # line 4:     """Function docstring."""
    # line 5:     x = 1
    # line 6:     y = 2
    # line 7:     return x + y
    # line 8: (blank)
    # line 9: class MyClass:
    # line 10:     def __init__(self):
    # line 11:         self.val = 42
    # line 12: (blank)
    # line 13:     def member_method(self):
    # line 14:         pass
    # line 15: (blank)
    # line 16: def another_func():
    # line 17:     pass
    content = """# module docstring

def standalone_func():
    \"\"\"Function docstring.\"\"\"
    x = 1
    y = 2
    return x + y

class MyClass:
    def __init__(self):
        self.val = 42

    def member_method(self):
        pass

def another_func():
    pass
"""
    write_file(temp_repo, "src/sample.py", content)
    defs = parse_repository(str(temp_repo))

    func1 = next((d for d in defs if d["name"] == "standalone_func"), None)
    cls1 = next((d for d in defs if d["name"] == "MyClass"), None)
    func2 = next((d for d in defs if d["name"] == "another_func"), None)

    assert func1 is not None
    assert func1["type"] == "function"
    assert func1["start_line"] == 3
    assert func1["end_line"] == 7

    assert cls1 is not None
    assert cls1["type"] == "class"
    assert cls1["start_line"] == 9
    assert cls1["end_line"] == 14

    assert func2 is not None
    assert func2["type"] == "function"
    assert func2["start_line"] == 16
    assert func2["end_line"] == 17


def test_nested_definitions_are_ignored(temp_repo):
    content = """
class OuterClass:
    class InnerClass:
        pass

    def outer_method(self):
        def inner_func():
            pass

def outer_function():
    def nested_func():
        return 1
    return nested_func()
"""
    write_file(temp_repo, "nested.py", content)
    defs = parse_repository(str(temp_repo))

    names = {d["name"] for d in defs}
    assert "OuterClass" in names
    assert "outer_function" in names

    # Nested classes and functions/methods must not be in top-level definitions
    assert "InnerClass" not in names
    assert "outer_method" not in names
    assert "inner_func" not in names
    assert "nested_func" not in names


def test_multiple_python_files_and_empty_files(temp_repo):
    write_file(temp_repo, "pkg/a.py", "def func_a():\n    pass\n")
    write_file(temp_repo, "pkg/b.py", "class ClassB:\n    pass\n")
    write_file(temp_repo, "pkg/empty.py", "")
    write_file(temp_repo, "pkg/whitespace.py", "   \n\n  \t \n")
    write_file(temp_repo, "pkg/non_python.txt", "def text_func(): pass")

    defs = parse_repository(str(temp_repo))
    names = {d["name"] for d in defs}

    assert names == {"func_a", "ClassB"}
    assert not any(d["file"].endswith("empty.py") for d in defs)
    assert not any(d["file"].endswith("whitespace.py") for d in defs)
    assert not any(d["file"].endswith("non_python.txt") for d in defs)


def test_syntax_errors_are_skipped(temp_repo):
    write_file(temp_repo, "valid.py", "def valid_func():\n    pass\n")
    write_file(temp_repo, "invalid.py", "def broken_syntax(:\n")

    defs = parse_repository(str(temp_repo))
    assert len(defs) == 1
    assert defs[0]["name"] == "valid_func"
