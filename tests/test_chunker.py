import tempfile
import shutil
from pathlib import Path
import pytest

from backend.chunker.python_chunker import chunk_file, chunk_repository


@pytest.fixture
def temp_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_chunk_file_semantic_units(temp_dir):
    code = """# Top module docstring
import os
import sys

API_KEY = "dummy"

def greet(name: str) -> str:
    \"\"\"Greeting function.\"\"\"
    return f"Hello {name}"

class Greeter:
    def __init__(self, prefix: str):
        self.prefix = prefix

    def say(self, name: str) -> str:
        return f"{self.prefix} {name}"
"""
    file_path = temp_dir / "sample.py"
    file_path.write_text(code, encoding="utf-8")

    chunks = chunk_file(str(file_path), max_chunk_lines=50)

    # We expect:
    # 1. module_level chunk (imports, constants, header)
    # 2. function chunk for greet
    # 3. class chunk for Greeter
    types = [c["chunk_type"] for c in chunks]
    assert "module_level" in types
    assert "function" in types
    assert "class" in types

    greet_chunk = next(c for c in chunks if c["symbol_name"] == "greet")
    assert "def greet(name: str) -> str:" in greet_chunk["content"]
    assert 'return f"Hello {name}"' in greet_chunk["content"]

    greeter_chunk = next(c for c in chunks if c["symbol_name"] == "Greeter")
    assert "class Greeter:" in greeter_chunk["content"]
    assert "def say(self, name: str)" in greeter_chunk["content"]


def test_chunk_file_large_function_sliding_window(temp_dir):
    # Construct a function with 50 lines
    lines = ["def huge_function():\n"]
    for i in range(49):
        lines.append(f"    val_{i} = {i}\n")
    lines.append("    return val_0\n")

    file_path = temp_dir / "huge.py"
    file_path.write_text("".join(lines), encoding="utf-8")

    # With max_chunk_lines=20 and overlap=5, this function should be split into multiple slices
    chunks = chunk_file(str(file_path), max_chunk_lines=20, overlap_lines=5)

    assert len(chunks) > 1
    slice_types = [c["chunk_type"] for c in chunks]
    assert all("function_slice" == t for t in slice_types)
    assert all(c["symbol_name"] == "huge_function" for c in chunks)


def test_chunk_repository(temp_dir):
    pkg = temp_dir / "mypkg"
    pkg.mkdir()

    (pkg / "a.py").write_text("def func_a():\n    return 'a'\n", encoding="utf-8")
    (pkg / "b.py").write_text("class ClassB:\n    pass\n", encoding="utf-8")
    (pkg / "empty.py").write_text("", encoding="utf-8")

    all_chunks = chunk_repository(str(temp_dir))
    names = {c["symbol_name"] for c in all_chunks if c["symbol_name"]}
    assert "func_a" in names
    assert "ClassB" in names

    # Sequential global indexing
    indexes = [c["chunk_index"] for c in all_chunks]
    assert indexes == list(range(len(all_chunks)))
