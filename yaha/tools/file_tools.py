import os
import re
import difflib
import fnmatch

from .output_guard import guard_output

def _coerce_line_number(value, name):
    """Tool-call args may arrive as strings (e.g. DSML parameters are always
    strings); line numbers must be ints for the range arithmetic below."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got {value!r}")

def list_files(path=".", pattern=None, access_controller=None, ui=None):
    """List files and directories in the specified path."""
    if access_controller:
        allowed, msg = access_controller.validate_path(path)
        if not allowed:
            return msg
            
    try:
        if not os.path.exists(path):
            return f"Path not found: {path}"
        
        entries = os.listdir(path)
        entries.sort()
        
        result = []
        for entry in entries:
            full_path = os.path.join(path, entry)
            entry_type = "d" if os.path.isdir(full_path) else "f"
            size = os.path.getsize(full_path) if os.path.isfile(full_path) else 0
            result.append(f"[{entry_type}] {entry} ({size} bytes)")
        
        output = "\n".join(result)
        return output
    except Exception as e:
        return f"Error listing files: {str(e)}"


def create_file(path, content, access_controller=None, ui=None):
    if access_controller:
        allowed, msg = access_controller.validate_path(path, mode='write')
        if not allowed:
            return msg
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"File created at {path}"
    except Exception as e:
        return f"Error creating file: {str(e)}"

def read_file(path, start_line, end_line, access_controller=None, ui=None,
              max_output_bytes=None):
    """Read a line range, capped to the caller's remaining-context budget.

    ``max_output_bytes`` is the byte cap computed by the agent tool dispatch
    (see tools/output_guard.py). The pre-existing line-range semantics run
    first; the guard is a last-resort backstop against a single read eating the
    whole context window, and it reports what it dropped. ``None`` means the
    caller opted out (direct callers keep the exact legacy behaviour).
    """
    if access_controller:
        allowed, msg = access_controller.validate_path(path)
        if not allowed:
            return msg
    try:
        start_line = _coerce_line_number(start_line, "start_line")
        end_line = _coerce_line_number(end_line, "end_line")
        if not os.path.exists(path):
            return f"File not found: {path}"

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        # Line numbers are 1-based
        start_idx = max(0, start_line - 1)
        end_idx = min(len(lines), end_line)
        
        content = "".join(lines[start_idx:end_idx])
        lines_read = end_idx - start_idx
        
        result = f"LINES READ: {lines_read}\n\n{content}"
        return guard_output(result, max_output_bytes)
    except Exception as e:
        return f"Error reading file: {str(e)}"

def write_file(path, content, start_line, access_controller=None, ui=None):
    if access_controller:
        allowed, msg = access_controller.validate_path(path, mode='write')
        if not allowed:
            return msg
    try:
        start_line = _coerce_line_number(start_line, "start_line")
        if not os.path.exists(path):
            return create_file(path, content, access_controller=access_controller, ui=ui)
        
        with open(path, "r", encoding="utf-8") as f:
            old_content = f.read()
            lines = old_content.splitlines(keepends=True)
        
        # Insert content at start_line (1-based)
        idx = max(0, start_line - 1)
        new_lines = content.splitlines(keepends=True)
        # Ensure new_lines has line endings if content didn't end with one but it's being inserted
        if content and not content.endswith('\n') and idx < len(lines):
             new_lines[-1] += '\n'
             
        lines[idx:idx] = new_lines
        new_content = "".join(lines)
        
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
            
        # Generate Diff
        diff = difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}"
        )
        diff_str = "".join(diff)
        if diff_str:
            return diff_str
        return f"Content written to {path} (no changes detected)."
    except Exception as e:
        return f"Error writing to file: {str(e)}"

def append_file(path, content, access_controller=None, ui=None):
    if access_controller:
        allowed, msg = access_controller.validate_path(path, mode='write')
        if not allowed:
            return msg
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
        return f"Content appended to {path}"
    except Exception as e:
        return f"Error appending to file: {str(e)}"

def search_files(path, search_text, access_controller=None, ui=None):
    if access_controller:
        allowed, msg = access_controller.validate_path(path)
        if not allowed:
            return msg
    try:
        if not os.path.exists(path):
            return f"File not found: {path}"
        
        results = []
        pattern = re.compile(search_text)
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if pattern.search(line):
                    results.append(f"{i}: {line.strip()}")
        
        if not results:
            return "No matches found."
        return "\n".join(results)
    except Exception as e:
        return f"Error searching file: {str(e)}"

def grep_files(path, search_text, recursive=True, glob_pattern=None, max_results=100, access_controller=None, ui=None):
    """Search files or directories for lines matching a regular expression."""
    if access_controller:
        allowed, msg = access_controller.validate_path(path)
        if not allowed:
            return msg
    try:
        if not os.path.exists(path):
            return f"Path not found: {path}"
        
        pattern = re.compile(search_text)
        results = []
        total = 0
        files = []
        
        if os.path.isfile(path):
            files = [path]
        elif os.path.isdir(path):
            if recursive:
                for root, dirs, filenames in os.walk(path):
                    for filename in filenames:
                        if glob_pattern and not fnmatch.fnmatch(filename, glob_pattern):
                            continue
                        files.append(os.path.join(root, filename))
            else:
                for entry in os.listdir(path):
                    full_path = os.path.join(path, entry)
                    if os.path.isfile(full_path):
                        if glob_pattern and not fnmatch.fnmatch(entry, glob_pattern):
                            continue
                        files.append(full_path)
            files.sort()
        
        cwd = os.getcwd()
        for file_path in files:
            if access_controller:
                allowed, msg = access_controller.validate_path(file_path)
                if not allowed:
                    continue
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    rel_path = os.path.relpath(file_path, cwd)
                    for i, line in enumerate(f, 1):
                        if pattern.search(line):
                            results.append(f"{rel_path}:{i}: {line.strip()}")
                            total += 1
                            if max_results and total >= max_results:
                                break
                    if max_results and total >= max_results:
                        break
            except Exception:
                continue
        
        if not results:
            return "No matches found."
        
        output = "\n".join(results)
        return output
    except Exception as e:
        return f"Error searching files: {str(e)}"

def edit_file(path, search_text=None, replace_text=None, start_line=None, end_line=None, access_controller=None, ui=None):
    if access_controller:
        allowed, msg = access_controller.validate_path(path, mode='write')
        if not allowed:
            return msg
    try:
        if not os.path.exists(path):
            return f"File not found: {path}"
        
        with open(path, "r", encoding="utf-8") as f:
            old_content = f.read()
        
        if search_text is not None:
            if search_text not in old_content:
                return f"Search text not found in {path}"
            if replace_text is None:
                return "Error: replace_text is required when using search_text."
            new_content = old_content.replace(search_text, replace_text)
        elif start_line is not None and end_line is not None:
            start_line = _coerce_line_number(start_line, "start_line")
            end_line = _coerce_line_number(end_line, "end_line")
            lines = old_content.splitlines(keepends=True)
            start = max(0, start_line - 1)
            end = min(len(lines), end_line)
            lines[start:end] = [replace_text] if replace_text.endswith('\n') else [replace_text + '\n']
            new_content = "".join(lines)
        else:
            return "Invalid edit_file parameters. Provide either search_text or start_line/end_line."
        
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
            
        # Generate Diff
        diff = difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}"
        )
        diff_str = "".join(diff)
        if diff_str:
            return diff_str
        return f"File {path} edited successfully (no changes detected)."
    except Exception as e:
        return f"Error editing file: {str(e)}"
