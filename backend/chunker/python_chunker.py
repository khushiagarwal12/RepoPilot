import os
from typing import List, Dict, Any, Optional
from backend.parser.python_ast import extract_definitions, relative_posix_path


def chunk_file(
    file_path: str,
    max_chunk_lines: int = 100,
    overlap_lines: int = 15,
    repo_root: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Chunks a single Python source file into semantic and windowed snippets.

    Chunking Strategy:
    1. Extract AST definitions (functions, classes) with accurate start and end line ranges.
    2. Extract whole definitions as discrete chunks when within max_chunk_lines.
    3. If a definition exceeds max_chunk_lines, split it using sliding window with line overlap.
    4. Capture top-level standalone code (e.g., imports, constants, setup) outside definitions.

    ``file_path`` in the returned chunks is relative to ``repo_root`` with ``/``
    separators (file name only if ``repo_root`` is omitted); it never depends on the
    process working directory.

    Returns a list of dicts:
        {
            "file_path": str,
            "chunk_index": int,
            "chunk_type": str,   # 'function', 'class', or 'module_level'
            "symbol_name": Optional[str],
            "start_line": int,
            "end_line": int,
            "content": str
        }
    """
    if not os.path.exists(file_path):
        return []

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (UnicodeDecodeError, OSError):
        return []

    if not lines:
        return []

    rel_path = relative_posix_path(file_path, repo_root)

    try:
        definitions = extract_definitions(file_path, repo_root)
    except Exception:
        # If parsing fails (e.g. syntax error), fall back to sliding-window line chunking
        definitions = []

    # Sort definitions by their start line
    definitions.sort(key=lambda d: d.get("start_line", 0))

    chunks: List[Dict[str, Any]] = []
    chunk_index = 0
    total_lines = len(lines)
    covered_lines = set()

    # 1. Process AST definitions
    for d in definitions:
        s_line = d["start_line"]  # 1-based
        e_line = min(d["end_line"], total_lines)
        for ln in range(s_line, e_line + 1):
            covered_lines.add(ln)

        def_lines_count = e_line - s_line + 1
        if def_lines_count <= max_chunk_lines:
            content = "".join(lines[s_line - 1 : e_line])
            if content.strip():
                chunks.append(
                    {
                        "file_path": rel_path,
                        "chunk_index": chunk_index,
                        "chunk_type": d["type"],
                        "symbol_name": d["name"],
                        "start_line": s_line,
                        "end_line": e_line,
                        "content": content,
                    }
                )
                chunk_index += 1
        else:
            # Definition is larger than max_chunk_lines: sliding window
            curr = s_line
            while curr <= e_line:
                window_end = min(curr + max_chunk_lines - 1, e_line)
                sub_content = "".join(lines[curr - 1 : window_end])
                if sub_content.strip():
                    chunks.append(
                        {
                            "file_path": rel_path,
                            "chunk_index": chunk_index,
                            "chunk_type": f"{d['type']}_slice",
                            "symbol_name": d["name"],
                            "start_line": curr,
                            "end_line": window_end,
                            "content": sub_content,
                        }
                    )
                    chunk_index += 1
                if window_end >= e_line:
                    break
                curr += max_chunk_lines - overlap_lines

    # 2. Collect uncovered lines (e.g., imports, globals, standalone scripts)
    uncovered_ranges: List[tuple[int, int]] = []
    start_uncovered = None

    for ln in range(1, total_lines + 1):
        if ln not in covered_lines:
            if start_uncovered is None:
                start_uncovered = ln
        else:
            if start_uncovered is not None:
                uncovered_ranges.append((start_uncovered, ln - 1))
                start_uncovered = None

    if start_uncovered is not None:
        uncovered_ranges.append((start_uncovered, total_lines))

    for start_r, end_r in uncovered_ranges:
        block_text = "".join(lines[start_r - 1 : end_r])
        # Skip chunks that only contain whitespace/empty lines
        if not block_text.strip():
            continue

        range_len = end_r - start_r + 1
        if range_len <= max_chunk_lines:
            chunks.append(
                {
                    "file_path": rel_path,
                    "chunk_index": chunk_index,
                    "chunk_type": "module_level",
                    "symbol_name": None,
                    "start_line": start_r,
                    "end_line": end_r,
                    "content": block_text,
                }
            )
            chunk_index += 1
        else:
            curr = start_r
            while curr <= end_r:
                window_end = min(curr + max_chunk_lines - 1, end_r)
                sub_content = "".join(lines[curr - 1 : window_end])
                if sub_content.strip():
                    chunks.append(
                        {
                            "file_path": rel_path,
                            "chunk_index": chunk_index,
                            "chunk_type": "module_level_slice",
                            "symbol_name": None,
                            "start_line": curr,
                            "end_line": window_end,
                            "content": sub_content,
                        }
                    )
                    chunk_index += 1
                if window_end >= end_r:
                    break
                curr += max_chunk_lines - overlap_lines

    # Sort all generated chunks chronologically by start_line and re-index sequentially
    chunks.sort(key=lambda c: (c["start_line"], c["end_line"]))
    for idx, c in enumerate(chunks):
        c["chunk_index"] = idx

    return chunks


def chunk_repository(repo_root: str, max_chunk_lines: int = 100, overlap_lines: int = 15) -> List[Dict[str, Any]]:
    """Traverses a repository root and generates chunks for all valid Python files."""
    all_chunks: List[Dict[str, Any]] = []
    global_index = 0

    for dirpath, _dirnames, filenames in os.walk(repo_root):
        for fname in filenames:
            if fname.endswith(".py"):
                full_path = os.path.join(dirpath, fname)
                file_chunks = chunk_file(full_path, max_chunk_lines, overlap_lines, repo_root)
                for chunk in file_chunks:
                    chunk["chunk_index"] = global_index
                    global_index += 1
                    all_chunks.append(chunk)

    return all_chunks
